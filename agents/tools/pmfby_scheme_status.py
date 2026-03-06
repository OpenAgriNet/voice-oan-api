import uuid
import json
from datetime import datetime, timezone
from helpers.utils import get_logger
import httpx
from app.config import get_default_httpx_timeout
from pydantic import BaseModel, AnyHttpUrl
from typing import List, Optional, Dict, Any, Literal, ClassVar
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.tools import RunContext
from agents.deps import FarmerContext
import os

logger = get_logger(__name__)


def generate_transaction_id(session_id: str, key: str) -> str:
    """Generate a consistent transaction ID for init and status calls."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, (session_id + key)))


def normalize_phone_for_api(phone: str) -> str:
    """Strip to digits only. BAP expects 10-digit Indian number (no country code), e.g. 9953947674."""
    digits = "".join(c for c in str(phone).strip() if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits if digits else phone.strip()

# -----------------------
# Basic Models
# -----------------------

# Set of PII codes that should be masked
PII_CODES: set[str] = {
    "account-number",           # Bank account number
    "bank-account-number",      # Alternative bank account field name
    "ifsc",                     # IFSC code
    "ifsc-code",               # Alternative IFSC field name  
    "aadharPaymentAccountNumber",  # Aadhar payment account
    "aadhar-account-number",   # Alternative aadhar account field name
    "mobile",                  # Mobile number
    "phone",                   # Phone number
    "contact",                 # Contact number
    "farmer-mobile",           # Farmer mobile number
    "registered-mobile"        # Registered mobile number
}

def mask_pii_value(value: str) -> str:
    """Apply unified PII masking to any sensitive value.
    Shows only last 4 digits, masks everything else with 'X'.
    Returns empty string for null/empty values.
    
    Masking Pattern:
    - Very short values (≤4 chars): completely mask with 'X'
    - Longer values (5+ chars): mask all except last 4 digits
    
    Examples:
    - "7350994908" → "XXXXXX4908"
    - "SBIN0001234" → "XXXXXXX1234"
    - "Test" → "XXXX"
    - "AB" → "XX"
    """
    if not value or value == "null" or value == "-":
        return ""
    
    # Remove whitespace for processing
    clean_value = str(value).strip()
    
    # For very short values (4 or fewer chars), completely mask
    if len(clean_value) <= 4:
        return "X" * len(clean_value)
    
    # For longer values, show only last 4 digits
    return "X" * (len(clean_value) - 4) + clean_value[-4:]

def format_value(value: Any, descriptor: Optional[Dict[str, Any]] = None) -> str:
    """Format any value as a string, with PII masking for sensitive fields"""
    if value is None or value == "" or value == "-":
        return ""
        
    # Convert to string
    str_value = str(value)
    
    # Check if this is a sensitive field that needs masking
    if descriptor:
        code = descriptor.get("code", "")
        if code and code.lower() in {pii_code.lower() for pii_code in PII_CODES}:
            return mask_pii_value(str_value)
        
    if isinstance(value, (int, float)):
        return str_value
    return str_value

class ListItem(BaseModel):
    descriptor: Dict[str, Any]
    value: Optional[Any] = None
    display: bool = True
    list: Optional[List[Dict[str, Any]]] = None

    class Config:
        extra = "allow"

    def __str__(self) -> str:
        name = self.descriptor.get("name")
        if name and self.value is not None:
            formatted_value = format_value(self.value, self.descriptor)
            # Skip empty values entirely
            if formatted_value:
                return f"{name}: {formatted_value}"
        return ""

class Tag(BaseModel):
    descriptor: Optional[Dict[str, Any]] = None
    value: Optional[Any] = None
    display: bool = True
    list: Optional[List[Dict[str, Any]]] = None

    class Config:
        extra = "allow"

    def __str__(self) -> str:
        if self.list:
            lines = []
            for item in self.list:
                if item.get("display", True):
                    desc = item.get("descriptor", {})
                    name = desc.get("name")
                    value = item.get("value")
                    if name and value is not None:
                        formatted_value = format_value(value, desc)
                        # Skip empty values entirely
                        if formatted_value:
                            lines.append(f"{name}: {formatted_value}")
                    nested_list = item.get("list", [])
                    for subitem in nested_list:
                        if subitem.get("display", True):
                            sub_desc = subitem.get("descriptor", {})
                            sub_name = sub_desc.get("name")
                            sub_value = subitem.get("value")
                            if sub_name and sub_value is not None:
                                formatted_sub_value = format_value(sub_value, sub_desc)
                                # Skip empty values entirely
                                if formatted_sub_value:
                                    lines.append(f"  {sub_name}: {formatted_sub_value}")
            return "\n".join(lines)
        elif self.value is not None:
            if self.descriptor:
                name = self.descriptor.get("name")
                formatted_value = format_value(self.value, self.descriptor)
                # Skip empty values entirely
                if formatted_value:
                    if name:
                        return f"{name}: {formatted_value}"
                    return f"{self.descriptor}: {formatted_value}"
            else:
                # Handle case where descriptor is None but we have a value
                formatted_value = format_value(self.value, None)
                if formatted_value:
                    return formatted_value
        return ""

class Item(BaseModel):
    id: str
    descriptor: Optional[Dict[str, Any]] = None
    tags: Optional[List[Tag]] = None

    class Config:
        extra = "allow"

    def __str__(self) -> str:
        lines = []
        if self.descriptor:
            name = self.descriptor.get("name")
            short_desc = self.descriptor.get("short_desc")
            long_desc = self.descriptor.get("long_desc")
            
            if name:
                lines.append(name)
            if short_desc:
                lines.append(short_desc)
            if long_desc:
                lines.append(long_desc)
        
        if self.tags:
            for tag in self.tags:
                tag_str = str(tag)
                if tag_str:
                    lines.append(tag_str)
        
        return "\n".join(lines)

class Provider(BaseModel):
    descriptor: Dict[str, Any]
    items: List[Item]

    class Config:
        extra = "allow"

    def __str__(self) -> str:
        lines = []
        if self.descriptor:
            name = self.descriptor.get("name")
            short_desc = self.descriptor.get("short_desc")
            
            if name:
                lines.append(f"Provider: {name}")
            if short_desc:
                lines.append(short_desc)
        
        for item in self.items:
            lines.append("\n" + "-" * 40)
            lines.append(str(item))
        
        return "\n".join(lines)

class Order(BaseModel):
    descriptor: Optional[Dict[str, Any]] = None
    providers: Optional[List[Provider]] = None
    # Handle alternative structure with provider (singular) and items directly
    provider: Optional[Dict[str, Any]] = None
    items: Optional[List[Item]] = None
    type: str = "DEFAULT"

    class Config:
        extra = "allow"

    def __str__(self) -> str:
        lines = []
        if self.descriptor:
            name = self.descriptor.get("name")
            if name:
                lines.append(f"# {name}")
        
        # Handle providers structure (original format)
        if self.providers:
            for provider in self.providers:
                provider_str = str(provider)
                if provider_str:
                    lines.append("\n" + provider_str)
        
        # Handle provider + items structure (new format)
        elif self.provider and self.items:
            provider_name = self.provider.get("id", "Unknown Provider")
            lines.append(f"Provider: {provider_name}")
            
            for item in self.items:
                lines.append("\n" + "-" * 40)
                lines.append(str(item))
        
        # Handle items only (fallback)
        elif self.items:
            for item in self.items:
                lines.append("\n" + "-" * 40)
                lines.append(str(item))
        
        return "\n".join(lines)

class Message(BaseModel):
    order: Order

    def __str__(self) -> str:
        return str(self.order)

class Context(BaseModel):
    domain: str
    action: str
    version: str
    timestamp: str
    message_id: str
    transaction_id: str
    ttl: Optional[str] = None
    bap_id: Optional[str] = None
    bap_uri: Optional[AnyHttpUrl] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[AnyHttpUrl] = None
    country: Optional[str] = None
    city: Optional[str] = None
    location: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"

class ResponseItem(BaseModel):
    context: Context
    message: Message

    def __str__(self) -> str:
        return str(self.message)

class StatusResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    class Config:
        extra = "allow"

    def _has_valid_data(self) -> bool:
        """Check if there are any responses with valid data (non-error)."""
        if not self.responses:
            return False
        
        for response in self.responses:
            order = response.message.order
            
            # Check for error conditions in items
            if order.items:
                for item in order.items:
                    if item.tags:
                        for tag in item.tags:
                            # If any tag has an error descriptor code, consider this an error response
                            if tag.descriptor and tag.descriptor.get("code") in ["farmer_id_not_found", "error", "not_found"]:
                                return False
                            # If descriptor name is "Error", also consider it an error
                            if tag.descriptor and tag.descriptor.get("name") == "Error":
                                return False
            
            # Check old structure (providers)
            if order.providers:
                for provider in order.providers:
                    if provider.items:
                        # Also check for errors in provider items
                        for item in provider.items:
                            if item.tags:
                                for tag in item.tags:
                                    if tag.descriptor and tag.descriptor.get("code") in ["farmer_id_not_found", "error", "not_found"]:
                                        return False
                                    if tag.descriptor and tag.descriptor.get("name") == "Error":
                                        return False
                        return True
            
            # Check new structure (provider + items directly) - only if no errors found
            elif order.provider and order.items:
                return True
            
            # Check items only structure - only if no errors found  
            elif order.items:
                return True
                
        return False

    def __str__(self) -> str:
        if not self._has_valid_data():
            return "No data available."
        return "\n".join(str(response) for response in self.responses if str(response))

class PMfbyInitRequest(BaseModel):
    """PMfbyInitRequest model for initiating PMFBY OTP (get_otp).
    
    Init only needs phone_number; inquiry_type, year, season are for status call.
    Matches BAP init spec: request_type=get_otp, provider=pmfby-agri, timestamp=Unix.
    """
    phone_number: str
    transaction_id: str

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        phone = normalize_phone_for_api(self.phone_number)
        # BAP init expects Unix timestamp as string (e.g. "1770634282")
        timestamp_str = str(int(now.timestamp()))
        return {
            "context": {
                "domain": "schemes:vistaar",
                "action": "init",
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("BPP_ID"),
                "bpp_uri": os.getenv("BPP_URI"),
                "transaction_id": self.transaction_id,
                "message_id": str(uuid.uuid4()),
                "timestamp": timestamp_str,
                "ttl": "PT10M",
                "location": {
                    "country": {"code": "IND"},
                    "city": {"code": "*"}
                }
            },
            "message": {
                "order": {
                    "provider": {"id": "pmfby-agri"},
                    "items": [{"id": "pmfby"}],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "tags": [
                                        {"descriptor": {"code": "request_type"}, "value": "get_otp"},
                                        {"descriptor": {"code": "phone_number"}, "value": phone}
                                    ]
                                },
                                "contact": {"phone": phone}
                            }
                        }
                    ]
                }
            }
        }


class PMfbyStatusWithOtpRequest(BaseModel):
    """PMfbyStatusWithOtpRequest model for status call with OTP validation.
    
    Args:
        order_id (str): OTP received via SMS (used as order_id in API)
        transaction_id (str): Same transaction_id used in init call
        inquiry_type: policy_status or claim_status
        year: Year for status
        season: Kharif, Rabi, or Summer
        phone_number: Phone number registered with scheme
    """
    order_id: str
    transaction_id: str
    inquiry_type: Literal["policy_status", "claim_status"]
    year: str
    season: Literal["Kharif", "Rabi", "Summer"]
    phone_number: str

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        # BAP expects Unix timestamp as string (e.g. "1770623357")
        timestamp_str = str(int(now.timestamp()))
        phone = normalize_phone_for_api(self.phone_number)
        return {
            "context": {
                "domain": "schemes:vistaar",
                "action": "status",
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("BPP_ID"),
                "bpp_uri": os.getenv("BPP_URI"),
                "transaction_id": self.transaction_id,
                "message_id": str(uuid.uuid4()),
                "timestamp": timestamp_str,
                "ttl": "PT10M",
                "location": {
                    "country": {"code": "IND"},
                    "city": {"code": "*"}
                }
            },
            "message": {
                "order_id": self.order_id,
                "order": {
                    "id": "order-1",
                    "provider": {"id": "pmfby-agri"},
                    "items": [{"id": "pmfby"}],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "tags": [
                                        {"descriptor": {"code": "inquiry_type"}, "value": self.inquiry_type},
                                        {"descriptor": {"code": "year"}, "value": self.year},
                                        {"descriptor": {"code": "season"}, "value": self.season}
                                    ]
                                },
                                "contact": {"phone": phone}
                            }
                        }
                    ]
                }
            }
        }

# -----------------------
# Functions
# -----------------------

def initiate_pmfby_status_check(ctx: RunContext[FarmerContext], phone_number: str) -> str:
    """Initiate PMFBY status check by sending OTP to farmer's mobile.
    
    Use this tool to initiate the process. Sends an OTP to the farmer's registered mobile.
    After OTP is received, use check_pmfby_status_with_otp with the OTP and inquiry details.
    
    Args:
        phone_number (str): Phone number registered with PMFBY (required)
    
    Returns:
        str: Response from the scheme init service (OTP sent confirmation)
    """
    try:
        session_id = ctx.deps.session_id
        transaction_id = generate_transaction_id(session_id, phone_number)
        
        payload = PMfbyInitRequest(
            phone_number=phone_number,
            transaction_id=transaction_id
        ).get_payload()
        
        endpoint = os.getenv("BAP_ENDPOINT").rstrip("/") + "/init"
        logger.info(f"[PMFBY INIT] Request URL: {endpoint}")
        logger.info(f"[PMFBY INIT] Request Payload: {json.dumps(payload, indent=2)}")
        
        response = httpx.post(
            endpoint,
            json=payload,
            timeout=get_default_httpx_timeout()
        )
        
        logger.info(f"[PMFBY INIT] Response Status: {response.status_code}")
        logger.info(f"[PMFBY INIT] Response Payload: {response.text}")
        
        if response.status_code != 200:
            logger.error(f"PMFBY init API returned status code {response.status_code}")
            return f"PMFBY init service unavailable. Status code: {response.status_code}"
        
        response_text = response.text.strip()
        if not response_text:
            return "OTP has been sent to your registered mobile. Please enter the OTP you received to see your policy/claim status."
        
        try:
            response_json = response.json()
            scheme_response = StatusResponse.model_validate(response_json)
            resp_str = str(scheme_response)
            # Return actual API response so user sees errors (e.g. phone not registered, invalid number)
            if resp_str and resp_str != "No data available.":
                return resp_str
            # Empty/minimal response usually means OTP sent
            return "OTP has been sent to your registered mobile. Please enter the OTP you received to see your policy/claim status."
        except json.JSONDecodeError:
            logger.warning(f"[PMFBY INIT] Non-JSON response: {response_text[:300]}")
            return "OTP has been sent to your registered mobile. Please enter the OTP you received to see your policy/claim status."
                
    except httpx.TimeoutException as e:
        logger.error(f"PMFBY init API request timed out: {str(e)}")
        return "PMFBY init request timed out. Please try again later."
    
    except httpx.RequestError as e:
        logger.error(f"PMFBY init API request failed: {e}")
        return f"PMFBY init request failed: {str(e)}"
    
    except Exception as e:
        logger.error(f"Error in PMFBY init: {e}")
        raise ModelRetry(f"Unexpected error in PMFBY init request. {str(e)}")


def check_pmfby_status_with_otp(
    ctx: RunContext[FarmerContext],
    otp: str,
    phone_number: str,
    inquiry_type: Literal["policy_status", "claim_status"],
    year: str,
    season: Literal["Kharif", "Rabi", "Summer"]
) -> str:
    """Check PMFBY status using OTP after initiating the OTP check.
    
    Use this tool to check policy or claim status using the OTP received via SMS
    after calling initiate_pmfby_status_check.
    
    Args:
        otp (str): 6-digit OTP received via SMS (used as order_id for API)
        phone_number (str): Phone number registered with PMFBY (required)
        inquiry_type (str): Type of inquiry - 'policy_status' or 'claim_status' (required)
        year (str): Year for which status is requested (required)
        season (str): Season - Kharif, Rabi, or Summer (required)
    
    Returns:
        str: Detailed scheme status information
    """
    try:
        otp_str = str(otp).strip() if otp else ""
        if not otp_str:
            raise ModelRetry("Invalid OTP. Please provide the OTP received via SMS.")
        # PMFBY OTP is 6 digits only
        digits = "".join(c for c in otp_str if c.isdigit())
        if len(digits) != 6:
            raise ModelRetry(
                "PMFBY OTP must be exactly 6 digits. Please ask the farmer for the 6-digit OTP they received on their mobile."
            )
        otp_str = digits

        session_id = ctx.deps.session_id
        # Use same transaction_id key as init (phone_number only)
        transaction_id = generate_transaction_id(session_id, phone_number)
        
        payload = PMfbyStatusWithOtpRequest(
            order_id=otp_str,
            transaction_id=transaction_id,
            inquiry_type=inquiry_type,
            year=year,
            season=season,
            phone_number=phone_number
        ).get_payload()
        
        endpoint = os.getenv("BAP_ENDPOINT").rstrip("/") + "/status"
        logger.info(f"[PMFBY STATUS] Request URL: {endpoint}")
        logger.info(f"[PMFBY STATUS] Request Payload: {json.dumps(payload, indent=2)}")
        
        response = httpx.post(
            endpoint,
            json=payload,
            timeout=get_default_httpx_timeout()
        )
        
        logger.info(f"[PMFBY STATUS] Response Status: {response.status_code}")
        logger.info(f"[PMFBY STATUS] Response Payload: {response.text}")
        
        if response.status_code != 200:
            logger.error(f"PMFBY status API returned status code {response.status_code}")
            return f"PMFBY status service unavailable. Status code: {response.status_code}"
        
        response_text = response.text.strip()
        if not response_text:
            return "PMFBY status service returned empty response. Please try again later."
        
        try:
            response_json = response.json()
            scheme_response = StatusResponse.model_validate(response_json)
            if not scheme_response._has_valid_data():
                record_type = "policy" if inquiry_type == "policy_status" else "claim"
                return f"No {record_type} record found for {year} {season}."
            return str(scheme_response)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from PMFBY status API: {e}")
            return "Invalid response from PMFBY status service. Please try again later."
                
    except httpx.TimeoutException as e:
        logger.error(f"PMFBY status API request timed out: {str(e)}")
        return "PMFBY status request timed out. Please try again later."
    
    except httpx.RequestError as e:
        logger.error(f"PMFBY status API request failed: {e}")
        return f"PMFBY status request failed: {str(e)}"
    
    except Exception as e:
        logger.error(f"Error in PMFBY status: {e}")
        raise ModelRetry(f"Unexpected error in PMFBY status request. {str(e)}")