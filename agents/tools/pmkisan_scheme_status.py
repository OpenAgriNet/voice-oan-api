import uuid
import json
from datetime import datetime, timezone
from helpers.utils import get_logger
import httpx
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any, Literal
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.tools import RunContext
from agents.deps import FarmerContext   
import os

logger = get_logger(__name__)

# -----------------------
# Basic Models (Shared)
# -----------------------

def generate_transaction_id(session_id: str, reg_no: str) -> str:
    """Common function to generate a transaction ID for the scheme status check.
    
    Args:
        session_id (str): Session ID to use as transaction ID
        reg_no (str): Registration number for the scheme
    
    Returns:
        str: A unique transaction ID
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, (session_id + reg_no)))

class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    short_desc: Optional[str] = None
    long_desc: Optional[str] = None

    def __str__(self) -> str:
        if self.name:
            return self.name
        elif self.code:
            return self.code
        return ""

class Contact(BaseModel):
    phone: Optional[str] = None

class Person(BaseModel):
    name: Optional[str] = None

class Customer(BaseModel):
    person: Optional[Person] = None
    contact: Optional[Contact] = None

# -----------------------
# Init Models
# -----------------------
class TagItem(BaseModel):
    descriptor: Descriptor
    value: Optional[str] = None
    display: bool = True

    def __str__(self) -> str:
        desc_name = self.descriptor.name or self.descriptor.code or "Tag"
        return f"{desc_name}: {self.value}" if self.value else desc_name

class Tag(BaseModel):
    display: bool = True
    descriptor: Descriptor
    list: Optional[List[TagItem]] = None

    def __str__(self) -> str:
        if self.list:
            items_str = "\n".join(str(tag_item) for tag_item in self.list)
            return items_str
        return str(self.descriptor)

class InitItem(BaseModel):
    id: str
    tags: Optional[List[Tag]] = None

    def __str__(self) -> str:
        lines = []
        if self.tags:
            for tag in self.tags:
                if tag.descriptor.short_desc:
                    lines.append(tag.descriptor.short_desc)
                elif tag.list:
                    lines.append(str(tag))
        return "\n".join(lines) if lines else f"Item ID: {self.id}"

class InitProvider(BaseModel):
    id: str

class InitOrder(BaseModel):
    provider: InitProvider
    items: List[InitItem]
    type: Optional[str] = None

    def __str__(self) -> str:
        lines = []
        for item in self.items:
            item_str = str(item)
            if item_str:
                lines.append(item_str)
        return "\n".join(lines)

class InitMessage(BaseModel):
    order: InitOrder

    def __str__(self) -> str:
        return str(self.order)

class InitContext(BaseModel):
    ttl: Optional[str] = None
    action: str
    timestamp: str
    message_id: str
    transaction_id: str
    domain: str
    version: str
    bap_id: Optional[str] = None
    bap_uri: Optional[AnyHttpUrl] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[AnyHttpUrl] = None
    country: Optional[str] = None
    city: Optional[str] = None

class InitResponseItem(BaseModel):
    context: InitContext
    message: InitMessage

    def __str__(self) -> str:
        return str(self.message)

class SchemeInitResponse(BaseModel):
    context: InitContext
    responses: List[InitResponseItem]

    def __str__(self) -> str:
        lines = []
        
        if not self.responses:
            lines.append("No response received from scheme service. Please try again later.")
            return "\n".join(lines)
            
        for response in self.responses:
            response_str = str(response)
            if response_str:
                lines.append(response_str)
        
        return "\n".join(lines) if lines else "OTP request processed. Please check your mobile for the OTP."

class SchemeInitRequest(BaseModel):
    """SchemeInitRequest model for initiating scheme status check.
    
    Args:
        registration_number (str): Registration number for the scheme
        transaction_id (str): Session ID to use as transaction ID
    """
    transaction_id: str
    registration_number: str
    phone_number: str = "" # TODO: Add phone number
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the SchemeInitRequest object to a dictionary.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the SchemeInitRequest object
        """
        now = datetime.now(timezone.utc)
        
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
                "timestamp": now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
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
                        "id": ""
                    },
                    "items": [
                        {
                            "id": ""
                        }
                    ],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "name": "Customer Name",
                                    "tags": [
                                        {
                                            "display": True,
                                            "descriptor": {
                                                "name": "Registration Details",
                                                "code": "reg-details"
                                            },
                                            "list": [
                                                {
                                                    "descriptor": {
                                                        "name": "Registration Number",
                                                        "code": "reg-number"
                                                    },
                                                    "value": self.registration_number,
                                                    "display": True
                                                }
                                            ]
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
# Status Response Models
# -----------------------
class StatusState(BaseModel):
    descriptor: Descriptor
    updated_at: Optional[str] = None

    def __str__(self) -> str:
        lines = []
        if self.descriptor.name:
            lines.append(f"Status: {self.descriptor.name}")
        if self.descriptor.short_desc:
            lines.append(f"Description: {self.descriptor.short_desc}")
        if self.descriptor.long_desc:
            lines.append(f"\nDetails:\n\n{self.descriptor.long_desc}")
        if self.updated_at:
            lines.append(f"\nLast Updated: {self.updated_at}")
        return "\n".join(lines)

class StatusFulfillment(BaseModel):
    customer: Optional[Customer] = None
    state: Optional[StatusState] = None
    tracking: Optional[bool] = None

    def __str__(self) -> str:
        lines = []
        if self.customer and self.customer.person and self.customer.person.name:
            lines.append(f"Customer: {self.customer.person.name}")
        if self.customer and self.customer.contact and self.customer.contact.phone:
            lines.append(f"Phone: {self.customer.contact.phone}")
        if self.state:
            lines.append(str(self.state))
        return "\n".join(lines)

class StatusTag(BaseModel):
    display: bool = True
    descriptor: Descriptor

    def __str__(self) -> str:
        if self.descriptor.short_desc:
            return str(self.descriptor.short_desc)
        else:
            return str(self.descriptor)

class StatusItem(BaseModel):
    id: str
    descriptor: Optional[Descriptor] = None

    def __str__(self) -> str:
        if self.descriptor:
            return str(self.descriptor)
        return f"Item ID: {self.id}"

class StatusProvider(BaseModel):
    id: str
    descriptor: Optional[Descriptor] = None

    def __str__(self) -> str:
        if self.descriptor:
            return str(self.descriptor)
        return f"Provider ID: {self.id}"

class StatusOrder(BaseModel):
    id: Optional[str] = None
    state: Optional[str] = None
    provider: Optional[StatusProvider] = None
    items: Optional[List[StatusItem]] = None
    fulfillments: Optional[List[StatusFulfillment]] = None
    tags: Optional[List[StatusTag]] = None
    type: Optional[str] = None

    def __str__(self) -> str:
        lines = []
        
        if self.id:
            lines.append(f"Order ID / OTP: {self.id}")
        if self.state:
            lines.append(f"State: **{self.state}**")
        
        if self.provider:
            lines.append(f"\nProvider: **{str(self.provider)}**")
        
        if self.items:
            lines.append(f"\nService: **{', '.join(str(item) for item in self.items)}**")
        
        if self.fulfillments:
            for fulfillment in self.fulfillments:
                fulfillment_str = str(fulfillment)
                if fulfillment_str:
                    lines.append(fulfillment_str)
        
        if self.tags:
            for tag in self.tags:
                tag_str = str(tag)
                lines.append(f"\n{tag_str}")
        
        return "\n".join(lines)

class StatusMessage(BaseModel):
    order: StatusOrder

    def __str__(self) -> str:
        return str(self.order)

class StatusContext(BaseModel):
    ttl: Optional[str] = None
    action: str
    timestamp: str
    message_id: str
    transaction_id: str
    domain: str
    version: str
    bap_id: Optional[str] = None
    bap_uri: Optional[AnyHttpUrl] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[AnyHttpUrl] = None
    country: Optional[str] = None
    city: Optional[str] = None

class StatusResponseItem(BaseModel):
    context: StatusContext
    message: StatusMessage

    def __str__(self) -> str:
        return str(self.message)

class SchemeStatusResponse(BaseModel):
    context: StatusContext
    responses: List[StatusResponseItem]

    def __str__(self) -> str:
        lines = []
        
        if not self.responses:
            lines.append("No status information received from scheme service. Please try again later.")
            return "\n".join(lines)
        
        for idx, response in enumerate(self.responses, 1):
            if len(self.responses) > 1:
                lines.append(f"\n--- Response {idx} ---")
            response_str = str(response)
            if response_str:
                lines.append(response_str)
        
        return "\n".join(lines) if lines else "No scheme status data available."

# -----------------------
# Request Model for Status
# -----------------------
class SchemeStatusRequest(BaseModel):
    """SchemeStatusRequest model for checking scheme status with OTP.
    
    Args:
        transaction_id (str): Session ID to use as transaction ID
        otp (str): OTP received via SMS (parameter is named order_id for API compatibility)
        registration_number (str): Registration number for the scheme
        phone_number (str): Phone number for the scheme (optional)
    """
    transaction_id: str
    otp: str
    # NOTE: These are not used but are retained for future compatibility
    registration_number: str 
    phone_number: str = ""
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the SchemeStatusRequest object to a dictionary.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the SchemeStatusRequest object
        """
        now = datetime.now(timezone.utc)
        
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
                "timestamp": now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
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
                "order_id": self.otp
            }
        }

# -----------------------
# Functions
# -----------------------
def initiate_pm_kisan_status_check(ctx: RunContext[FarmerContext], reg_no: str) -> str:
    """Initiate PM Kisan status check by sending OTP to farmer's mobile.
    
    Use this tool to initiate the process to check the status of a farmer's scheme benefit by sending an OTP to their registered mobile number.

    Args:
        reg_no (str): PM Kisan registration number, usually a 11 digit string such (2 digit state code + 9 digit unique number)

    Returns:
        str: Response from the scheme status check service
    """
    try:
        # Get session_id from context
        session_id = ctx.deps.session_id
        transaction_id = generate_transaction_id(session_id, reg_no)
        logger.info(f"Transaction ID: {transaction_id}")
        payload = SchemeInitRequest(
            registration_number=reg_no,
            transaction_id=transaction_id 
            # NOTE: Adding registration number as well - in case a person checks status for multiple farmers
        ).get_payload()
        
        response = httpx.post(
            os.getenv("BAP_ENDPOINT").rstrip("/") + "/init",
            json=payload,
            timeout=httpx.Timeout(10.0, read=15.0)
        )
        
        if response.status_code != 200:
            logger.error(f"Scheme init API returned status code {response.status_code}")
            return f"Scheme init service unavailable. Status code: {response.status_code}"
        
        # Check if response body is empty
        response_text = response.text.strip()
        if not response_text:
            logger.error("Scheme init API returned empty response")
            return "Scheme init service returned empty response. Please try again later."
        
        try:
            response_json = response.json()
            scheme_response = SchemeInitResponse.model_validate(response_json)
            return str(scheme_response)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response from scheme init API: {e}. Response text: {response_text[:200]}")
            return f"Invalid response from scheme init service. Please try again later."
                
    except httpx.TimeoutException as e:
        logger.error(f"Scheme init API request timed out: {str(e)}")
        return "Scheme init request timed out. Please try again later."
    
    except httpx.RequestError as e:
        logger.error(f"Scheme init API request failed: {e}")
        return f"Scheme init request failed: {str(e)}"
    
    except UnexpectedModelBehavior as e:
        logger.warning("Scheme init request exceeded retry limit")
        return "Scheme init service is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error in scheme init: {e}")
        raise ModelRetry(f"Unexpected error in scheme init request. {str(e)}")

def check_pm_kisan_status_with_otp(ctx: RunContext[FarmerContext], otp: str, reg_no: str) -> str:
    """Check PM Kisan status using OTP after initiating the OTP check.
     
    Use this tool to check the status of a farmer's PM Kisan benefit using the OTP received via SMS after calling initiate_pm_kisan_status_check.
 
    Args:
        otp (str): A 4 digit OTP received via SMS
        reg_no (str): PM Kisan registration number, usually a 11 digit string such (2 digit state code + 9 digit unique number)
 
    Returns:
        str: Detailed scheme status information including beneficiary details, payment status, and any issues or next steps
    """
    try:
        # Validate OTP format - must be exactly 4 digits
        if not otp.isdigit() or len(otp) != 4:
            raise ModelRetry("Invalid OTP format. Please provide a 4-digit OTP received via SMS.")
        # Get session_id from context
        session_id = ctx.deps.session_id
        transaction_id = generate_transaction_id(session_id, reg_no)
        logger.info(f"Transaction ID: {transaction_id}")
        payload = SchemeStatusRequest(transaction_id=transaction_id,
                                      otp=otp,  
                                      registration_number=reg_no,
                                      ).get_payload()
        
        response = httpx.post(
            os.getenv("BAP_ENDPOINT").rstrip("/") + "/status",
            json=payload,
            timeout=httpx.Timeout(10.0, read=15.0)
        )
        
        if response.status_code != 200:
            logger.error(f"Scheme status API returned status code {response.status_code}")
            return f"Scheme status service unavailable. Status code: {response.status_code}"
        
        # Check if response body is empty
        response_text = response.text.strip()
        if not response_text:
            logger.error("Scheme status API returned empty response")
            return "Scheme status service returned empty response. Please try again later."
        
        try:
            response_json = response.json()
            scheme_response = SchemeStatusResponse.model_validate(response_json)
            return str(scheme_response)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response from scheme status API: {e}. Response text: {response_text[:200]}")
            return f"Invalid response from scheme status service. Please try again later."
                
    except httpx.TimeoutException as e:
        logger.error(f"Scheme status API request timed out: {str(e)}")
        return "Scheme status request timed out. Please try again later."
    
    except httpx.RequestError as e:
        logger.error(f"Scheme status API request failed: {e}")
        return f"Scheme status request failed: {str(e)}"
    
    except UnexpectedModelBehavior as e:
        logger.warning("Scheme status request exceeded retry limit")
        return "Scheme status service is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error in scheme status: {e}")
        raise ModelRetry(f"Unexpected error in scheme status request. {str(e)}") 