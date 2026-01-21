"""
PM-KISAN Grievance Tools (create_grievance, check_grievance_status)

- Strongly typed with Pydantic
- Centralized crypto + transport
- Clear request builders
- Safe logging & timeouts
- DRY formatting for user-facing strings
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple, Literal

import httpx
from pydantic import BaseModel, Field, AnyUrl, ValidationError
from pydantic_ai import ModelRetry
from helpers.utils import get_logger
from helpers.encryption import hex_to_bytes, encrypt_aes_gcm, decrypt_aes_gcm

logger = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Config / Constants
# --------------------------------------------------------------------------------------

# TODO: Integrate this with OpenAgriNet network
GRIEVANCE_BASE_URL = os.getenv("GRIEVANCE_BASE_URL")  # e.g. https://pmkisan.gov.in/api
GRIEVANCE_TOKEN = os.getenv("GRIEVANCE_TOKEN", "PMK_123456")  # server expects a static token
KEY_1_HEX = os.getenv("GRIEVANCE_KEY_1")
KEY_2_HEX = os.getenv("GRIEVANCE_KEY_2")

# Mapping file: human-friendly grievance labels -> backend codes
_GRIEVANCE_JSON_PATH = os.getenv("GRIEVANCE_TYPES_PATH", "assets/grievance_types.json")


def _load_grievance_mapping(path: str) -> Dict[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("grievance_types.json must be an object of {label: code}")
            return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.error(f"Failed to load grievance mapping at '{path}': {e}")
        return {}


GRIEVANCE_MAPPING: Dict[str, str] = _load_grievance_mapping(_GRIEVANCE_JSON_PATH)
GRIEVANCE_TYPES: List[str] = list(GRIEVANCE_MAPPING.keys())


# --------------------------------------------------------------------------------------
# Transport + Crypto
# --------------------------------------------------------------------------------------

class Crypto(BaseModel):
    key: bytes
    iv: bytes

    @classmethod
    def from_env(cls) -> "Crypto":
        if not KEY_1_HEX or not KEY_2_HEX:
            raise ModelRetry("Grievance crypto keys not configured. Set GRIEVANCE_KEY_1 and GRIEVANCE_KEY_2.")
        return cls(key=hex_to_bytes(KEY_1_HEX), iv=hex_to_bytes(KEY_2_HEX))

    def encrypt_payload(self, payload: Dict[str, Any]) -> Dict[str, str]:
        plaintext = json.dumps(payload, ensure_ascii=False)
        encrypted = encrypt_aes_gcm(plaintext, self.key, self.iv)
        return {"EncryptedRequest": encrypted}

    def decrypt_envelope_output(self, encrypted_output: str) -> Dict[str, Any]:
        plaintext = decrypt_aes_gcm(encrypted_output, self.key, self.iv)
        return json.loads(plaintext)


class GrievanceClient(BaseModel):
    base_url: AnyUrl
    token: str = Field(default=GRIEVANCE_TOKEN)
    crypto: Crypto

    timeout_connect: int = 20
    timeout_read: int = 30

    @classmethod
    def from_env(cls) -> "GrievanceClient":
        if not GRIEVANCE_BASE_URL:
            raise ModelRetry("Grievance service base URL not configured. Set GRIEVANCE_BASE_URL.")
        return cls(base_url=GRIEVANCE_BASE_URL, crypto=Crypto.from_env())

    def _post(self, path: str, body: Dict[str, Any]) -> httpx.Response:
        url = f"{str(self.base_url).rstrip('/')}/{path.lstrip('/')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        encrypted_body = self.crypto.encrypt_payload(body)
        logger.info(f"Grievance API request: {url} | body: {json.dumps(encrypted_body)}")
        resp = httpx.post(url, json=encrypted_body, headers=headers, timeout=httpx.Timeout(self.timeout_connect, read=self.timeout_read))
        logger.info(f"Grievance API response: {url} | status: {resp.status_code} | body: {resp.text[:500]}")
        return resp

    def post_encrypted(self, path: str, body: Dict[str, Any]) -> "ServiceEnvelope":
        resp = self._post(path, body)
        if resp.status_code != 200:
            logger.error(f"Grievance API {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
            raise ModelRetry(f"Grievance service unavailable (HTTP {resp.status_code}).")
        try:
            env = ServiceEnvelope.model_validate(resp.json())
        except ValidationError as e:
            logger.error(f"Invalid grievance envelope for {path}: {e}")
            raise ModelRetry("Grievance service returned an invalid response envelope.")
        return env


# --------------------------------------------------------------------------------------
# Service Envelope (encrypted response container)
# --------------------------------------------------------------------------------------

class EncryptedData(BaseModel):
    type_: str = Field(..., alias="__type")
    output: str  # base64 string

    class Config:
        populate_by_name = True


class ServiceEnvelope(BaseModel):
    d: EncryptedData

    def decrypt(self, crypto: Crypto) -> Dict[str, Any]:
        return crypto.decrypt_envelope_output(self.d.output)


# --------------------------------------------------------------------------------------
# Decrypted response schemas
# --------------------------------------------------------------------------------------

class AadhaarTokenResponse(BaseModel):
    Responce: str  # service returns "True"/"False" spelled oddly
    AadhaarToken: Optional[str] = None
    message: Optional[str] = None

    def ok(self) -> bool:
        return str(self.Responce).lower() == "true"


class GrievanceStatusDetail(BaseModel):
    Reg_No: Optional[str] = None
    GrievanceDate: Optional[str] = None
    GrievanceDescription: Optional[str] = None
    OfficerReply: Optional[str] = None
    OfficeReplyDate: Optional[str] = None

    def __str__(self) -> str:
        lines: List[str] = []
        if self.Reg_No:
            lines.append(f"Registration Number: {self.Reg_No}")
        lines.append("Grievance Details:")
        if self.GrievanceDate:
            lines.append(f"  Date: {self.GrievanceDate}")
        if self.GrievanceDescription:
            lines.append(f"  Description: {self.GrievanceDescription}")
        lines.append("Officer Response:")
        if self.OfficerReply:
            lines.append(f"  Reply: {self.OfficerReply}")
            if self.OfficeReplyDate:
                lines.append(f"  Reply Date: {self.OfficeReplyDate}")
        else:
            lines.append("  Reply: Not yet responded")
        return "\n".join(lines)


class GrievanceStatusPayload(BaseModel):
    Responce: str
    message: Optional[str] = None
    details: Optional[List[GrievanceStatusDetail]] = None

    def ok(self) -> bool:
        return str(self.Responce).lower() == "true"

    def __str__(self) -> str:
        if not self.ok():
            return self.message or "No grievances found for this registration number."
        if not self.details:
            return "No grievance details available."
        # Service typically returns a list; use the first record
        return str(self.details[0])


class GenericMessageResponse(BaseModel):
    Responce: Optional[str] = None
    message: Optional[str] = None

    def ok(self) -> bool:
        return str(self.Responce).lower() == "true" if self.Responce is not None else True

    def __str__(self) -> str:
        if self.message:
            return self.message
        return "Success" if self.ok() else "Failed"


# --------------------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------------------

def _is_aadhaar(identity_no: str) -> bool:
    return identity_no.isdigit() and len(identity_no) == 12


def _aadhaar_token(client: GrievanceClient, identity_no: str) -> Optional[str]:
    body = {
        "Type": "IdentityNo_Details",
        "TokenNo": client.token,
        "IdentityNo": identity_no,
    }
    env = client.post_encrypted("/GrievanceAadhaarToken", body)
    data = AadhaarTokenResponse.model_validate(env.decrypt(client.crypto))
    if data.ok() and data.AadhaarToken:
        return data.AadhaarToken
    logger.warning(f"Aadhaar token lookup failed: {data.message or 'unknown error'}")
    return None


def _resolve_identity(client: GrievanceClient, identity_no: str, purpose: Literal["create", "status"]) -> Tuple[str, str]:
    """
    Returns (actual_identity_value, type_field)
    - For Aadhaar, get token and use Type 'IdentityNo_Details' (create) or 'IdentityNo_Status' (status)
    - For Reg_No, use Type 'Reg_No_Details' (create) or 'Reg_No_Status' (status)
    """
    if _is_aadhaar(identity_no):
        token = _aadhaar_token(client, identity_no)
        if not token:
            raise ModelRetry(
                "The provided Aadhaar number is not registered with PM-KISAN. "
                "Please provide the Aadhaar number registered with PM-KISAN or your PM-KISAN registration number."
            )
        type_map = {"create": "IdentityNo_Details", "status": "IdentityNo_Status"}
        return token, type_map[purpose]
    else:
        type_map = {"create": "Reg_No_Details", "status": "Reg_No_Status"}
        return identity_no, type_map[purpose]


# --------------------------------------------------------------------------------------
# Request builders
# --------------------------------------------------------------------------------------

class CreateGrievanceRequest(BaseModel):
    Type: str
    TokenNo: str
    IdentityNo: str
    GrievanceType: str
    GrievanceDescription: str

    @classmethod
    def build(
        cls,
        type_field: str,
        token_no: str,
        identity_value: str,
        grievance_code: str,
        grievance_description: str,
    ) -> "CreateGrievanceRequest":
        return cls(
            Type=type_field,
            TokenNo=token_no,
            IdentityNo=identity_value,
            GrievanceType=grievance_code,
            GrievanceDescription=grievance_description,
        )


class StatusRequest(BaseModel):
    Type: str
    TokenNo: str
    IdentityNo: str

    @classmethod
    def build(cls, type_field: str, token_no: str, identity_value: str) -> "StatusRequest":
        return cls(Type=type_field, TokenNo=token_no, IdentityNo=identity_value)


# --------------------------------------------------------------------------------------
# Formatters (user-facing strings)
# --------------------------------------------------------------------------------------

def _format_status(payload: Dict[str, Any]) -> str:
    try:
        status = GrievanceStatusPayload.model_validate(payload)
        return str(status)
    except ValidationError:
        # Fallback to generic interpretation
        msg = payload.get("message")
        if msg:
            return msg
        return "No grievance records found."


# --------------------------------------------------------------------------------------
# Exported Tools
# --------------------------------------------------------------------------------------

def submit_grievance(identity_no: str, grievance_description: str, grievance_type: str) -> str:
    """
    Create and submit a grievance to the PM-KISAN portal.

    Args:
        identity_no: PM-KISAN Registration Number (11-character alphanumeric string) or 12-digit Aadhaar number registered with PM-KISAN.
        grievance_description: Description of the grievance (plain text).
        grievance_type: Human-friendly grievance label. Must be one of the keys in GRIEVANCE_MAPPING.

    Returns:
        A user-friendly message summarizing submission outcome.
    """
    try:
        if not grievance_type or grievance_type not in GRIEVANCE_MAPPING:
            choices = '", "'.join(GRIEVANCE_TYPES)
            raise ModelRetry(f'Invalid grievance type: "{grievance_type}". Please select from: "{choices}".')

        if not grievance_description or len(grievance_description.strip()) < 10:
            raise ModelRetry("Please provide a brief grievance description.")

        client = GrievanceClient.from_env()
        identity_value, type_field = _resolve_identity(client, identity_no.strip(), purpose="create")

        body = CreateGrievanceRequest.build(
            type_field=type_field,
            token_no=client.token,
            identity_value=identity_value,
            grievance_code=GRIEVANCE_MAPPING[grievance_type],
            grievance_description=grievance_description.strip(),
        ).model_dump()

        logger.info("Submitting grievance (redacting IdentityNo from logs).")
        # IMPORTANT: do not log body with IdentityNo in plaintext
        env = client.post_encrypted("/LodgeGrievance", body)
        decrypted = env.decrypt(client.crypto)

        # Typical response carries {"message": "..."} and sometimes "Responce"
        try:
            msg = GenericMessageResponse.model_validate(decrypted)
            return str(msg)
        except ValidationError:
            # Fallback if schema differs
            return decrypted.get("message") or "Grievance submitted successfully."

    except httpx.TimeoutException:
        logger.error("Grievance submission timed out.")
        return "Grievance submission timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Grievance submission network error: {e}")
        return "Unable to reach grievance service. Please try again."
    except ModelRetry as e:
        # Bubble up actionable guidance to the agent/user
        return str(e)
    except Exception as e:
        logger.error(f"Unexpected error in create_grievance: {e}")
        raise ModelRetry(f"Unexpected error while submitting grievance. {str(e)}")


def grievance_status(identity_no: str) -> str:
    """
    Check grievance status by PM-KISAN Registration Number or Aadhaar (registered).

    Args:
        identity_no: PM-KISAN Registration Number (11-character alphanumeric string) or 12-digit Aadhaar registered with PM-KISAN.

    Returns:
        A user-friendly status string (registration number, dates, officer reply, etc.),
        or an explanatory message if not found yet.
    """
    try:
        client = GrievanceClient.from_env()
        identity_value, type_field = _resolve_identity(client, identity_no.strip(), purpose="status")

        body = StatusRequest.build(
            type_field=type_field,
            token_no=client.token,
            identity_value=identity_value,
        ).model_dump()

        env = client.post_encrypted("/GrievanceStatusCheck", body)
        decrypted = env.decrypt(client.crypto)
        return _format_status(decrypted)

    except httpx.TimeoutException:
        logger.error("Grievance status check timed out.")
        return "Grievance status check timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Grievance status network error: {e}")
        return "Unable to reach grievance service. Please try again."
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"Unexpected error in check_grievance_status: {e}")
        raise ModelRetry(f"Unexpected error while checking grievance status. {str(e)}")