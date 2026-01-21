import uuid
from datetime import datetime, timezone
from helpers.utils import get_logger
import httpx
from pydantic import BaseModel, AnyHttpUrl
from typing import List, Optional, Dict, Any, Literal, ClassVar
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
import os

logger = get_logger(__name__)

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

class PMfbyStatusRequest(BaseModel):
    """PMfbyStatusRequest model for checking PFMBY scheme status.
    
    Args:
        inquiry_type (str): Type of inquiry - 'policy_status' or 'claim_status' (required)
        year (str): Year for which status is requested (required)
        season (str): Season for which status is requested (required):
            - "Kharif" - Kharif season
            - "Rabi" - Rabi season
            - "Summer" - Summer season
        phone_number (str): Phone number registered with the scheme (required)
    """
    inquiry_type: Literal["policy_status", "claim_status"]  # Required field, no default
    year: str  # Required field, no default
    season: Literal["Kharif", "Rabi", "Summer"]  # Required field, no default
    phone_number: str  # Required field, no default
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the PMfbyStatusRequest object to a dictionary.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the PMfbyStatusRequest object
        """
        now = datetime.now(timezone.utc)
        # Convert to Unix timestamp (seconds since epoch) as string
        unix_timestamp = str(int(now.timestamp()))
        
        return {
            "context": {
                "domain": "schemes:vistaar",
                "action": "init",
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("BPP_ID"),
                "bpp_uri": os.getenv("BPP_URI"),
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "timestamp": unix_timestamp,
                "ttl": "PT10M",
                "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                }
            },
            "message": {
                "order": {
                    "provider": {
                        "id": "schemes-agri"
                    },
                    "items": [
                        {
                            "id": "pmfby"
                        }
                    ],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "tags": [
                                        {
                                            "descriptor": {
                                                "code": "inquiry_type"
                                            },
                                            "value": self.inquiry_type
                                        },
                                        {
                                            "descriptor": {
                                                "code": "year"
                                            },
                                            "value": self.year
                                        },
                                        {
                                            "descriptor": {
                                                "code": "season"
                                            },
                                            "value": self.season
                                        }
                                    ]
                                },
                                "contact": {
                                    "phone": self.phone_number
                                }
                            }
                        }
                    ]
                }
            }
        }

# -----------------------
# Functions
# -----------------------

def check_pmfby_status(
    phone_number: str,
    inquiry_type: Literal["policy_status", "claim_status"],
    year: str,
    season: Literal["Kharif", "Rabi", "Summer"]
) -> str:
    """Check PFMBY scheme status (policy or claim).
    
    Use this tool to check either policy status or claim status for a farmer's PFMBY scheme.
    You must ask the user for inquiry_type, year, season, and phone_number if not provided.
    
    Args:
        phone_number (str): Phone number registered with the scheme (required)
        inquiry_type (str): Type of inquiry - 'policy_status' or 'claim_status' (required). You must ask the user which type of inquiry they want.
        year (str): Year for which status is requested (required). You must ask the user for the year.
        season (str): Season for which status is requested (required). You must ask the user for the season (Kharif, Rabi, or Summer).
    
    Returns:
        str: Detailed scheme status information based on the inquiry type
    """
    try:
        payload = PMfbyStatusRequest(
            inquiry_type=inquiry_type,
            year=year,
            season=season,
            phone_number=phone_number
        ).get_payload()
        
        response = httpx.post(
            os.getenv("BAP_ENDPOINT").rstrip("/") + "/init",
            json=payload,
            timeout=httpx.Timeout(10.0, read=15.0)
        )
        
        if response.status_code != 200:
            logger.error(f"PFMBY status API returned status code {response.status_code}")
            return f"PFMBY status service unavailable. Status code: {response.status_code}"
            
        scheme_response = StatusResponse.model_validate(response.json())
        return str(scheme_response)
                
    except httpx.TimeoutException as e:
        logger.error(f"PFMBY status API request timed out: {str(e)}")
        return "PFMBY status request timed out. Please try again later."
    
    except httpx.RequestError as e:
        logger.error(f"PFMBY status API request failed: {e}")
        return f"PFMBY status request failed: {str(e)}"
    
    except UnexpectedModelBehavior as e:
        logger.warning("PFMBY status request exceeded retry limit")
        return "PFMBY status service is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error in PFMBY status: {e}")
        raise ModelRetry(f"Unexpected error in PFMBY status request. {str(e)}")