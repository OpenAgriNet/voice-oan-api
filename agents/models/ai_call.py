"""AI call (artificial insemination) booking models — ported from chat backend."""
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator


# Partner records prefix the technician's name with an internal society/route
# number — "1712 NARENDRAKUMAR-NARAYANDAS-PANDOR". The caller has no use for it
# and TTS reads it aloud, sometimes as a time of day ("seventeen thirty
# Sanjaykumar..."), so it is stripped everywhere a name reaches the model or the
# caller. Measured over 54,014 technician options presented on prod (21-24 Sep):
# 20.8% carry the prefix, no name has digits anywhere else, and none is digits
# only — so an anchored leading-number strip cannot damage a real name.
_AIT_NAME_CODE_PREFIX_RE = re.compile(r"^\s*\d+\s*[-\s]\s*")


def strip_ait_name_code_prefix(value: str | None) -> str | None:
    """Drop a leading internal number from a technician name.

    Returns the original when stripping would leave nothing, so a record that is
    somehow all digits still identifies someone rather than becoming blank.
    """
    if not value:
        return value
    stripped = _AIT_NAME_CODE_PREFIX_RE.sub("", value, count=1).strip()
    return stripped or value


class AISpecies(str, Enum):
    COW = "cow"
    BUFFALO = "buffalo"

    @property
    def encrypted_species_id(self) -> str:
        if self is AISpecies.COW:
            return "/cT4TzbfxFOo+L+ZN9x1ZQ=="
        return "M/3Ahr/kOi5ks+Bb5w2uoA=="


class AICallRequestModel(BaseModel):
    union_code: str = Field(..., alias="unionCode")
    society_code: str = Field(..., alias="societyCode")
    farmer_code: str = Field(..., alias="farmerCode")
    user_id: str = Field(..., alias="userId")
    species: AISpecies

    def to_query_params(self) -> dict[str, str]:
        return {
            "unionCode": self.union_code,
            "societyCode": self.society_code,
            "farmerCode": self.farmer_code,
            "userId": self.user_id,
            "speciesId": self.species.encrypted_species_id,
        }


class AICallResponseModel(BaseModel):
    ait_name: str | None = Field(None, alias="aitName")
    ticket_number: str | None = Field(None, alias="ticketNumber")

    @staticmethod
    def _normalize_phone(value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if digits.startswith("91") and len(digits) > 10:
            digits = digits[2:].lstrip("0") or digits
        return digits.lstrip("0") or value

    @classmethod
    def _normalize_ait_phone(cls, value: str) -> str:
        if "(" not in value:
            return value
        phone_part, rest = value.split("(", 1)
        normalized_phone = cls._normalize_phone(phone_part.strip())
        return f"{normalized_phone}({rest}"

    @field_validator("ait_name", mode="before")
    @classmethod
    def normalize_ait_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = cls._normalize_ait_phone(value)
        if "(" in normalized:
            # "<phone>(<name>)" — the code rides on the name inside the parens,
            # and the leading digits are the phone, which must survive.
            phone_part, rest = normalized.split("(", 1)
            return f"{phone_part}({strip_ait_name_code_prefix(rest)}"
        return strip_ait_name_code_prefix(normalized)
