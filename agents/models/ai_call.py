"""AI call (artificial insemination) booking models — ported from chat backend."""
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator


# Partner records prefix the technician's name with an internal society/route
# number — "1712 NARENDRAKUMAR-NARAYANDAS-PANDOR". The caller has no use for it
# and TTS reads it aloud, sometimes as a time of day ("seventeen thirty
# Sanjaykumar..."), so every digit is dropped before a name reaches the model or
# the caller.
#
# Dropping all digits rather than only a leading run is deliberate. Across 5,263
# distinct technician names seen on prod over 30 days, 433 carry a leading code
# and exactly one holds a digit anywhere else — "S0MJIBHAI-HEMTABHAI-CHAUDHARY",
# which is SOMJIBHAI with the letter O mistyped as a zero. There is no such thing
# as a legitimate digit in these names, so nothing here can be lost, and the rule
# survives a code that arrives in a position we have not seen.
_AIT_NAME_DIGITS_RE = re.compile(r"\d+")
_AIT_NAME_EDGE_SEPARATORS_RE = re.compile(r"^[\s\-]+|[\s\-]+$")
_AIT_NAME_INNER_SPACE_RE = re.compile(r"\s{2,}")


def strip_ait_name_codes(value: str | None) -> str | None:
    """Drop internal numbers from a technician name and tidy what they leave.

    Returns the original when stripping would leave nothing, so a record that is
    somehow all digits still identifies someone rather than becoming blank.
    """
    if not value:
        return value
    cleaned = _AIT_NAME_DIGITS_RE.sub("", value)
    cleaned = _AIT_NAME_EDGE_SEPARATORS_RE.sub("", cleaned)
    cleaned = _AIT_NAME_INNER_SPACE_RE.sub(" ", cleaned).strip()
    return cleaned or value


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
            return f"{phone_part}({strip_ait_name_codes(rest)}"
        return strip_ait_name_codes(normalized)
