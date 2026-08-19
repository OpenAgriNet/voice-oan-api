import os
import uuid
from datetime import datetime
from helpers.utils import get_logger
import httpx
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any
from pydantic_ai import ModelRetry, UnexpectedModelBehavior, RunContext
from agents.deps import FarmerContext
from agents.tools.common import get_nudge_message, send_nudge_message_raya
from dotenv import load_dotenv

load_dotenv()

logger = get_logger(__name__)

# -----------------------
# Basic Models
# -----------------------
class Image(BaseModel):
    url: AnyHttpUrl

class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    short_desc: Optional[str] = None
    long_desc: Optional[str] = None
    images: Optional[List[Image]] = None

    def __str__(self) -> str:
        if self.name:
            return self.name
        elif self.code:
            return self.code
        return ""

class Country(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None

class Location(BaseModel):
    country: Optional[Country] = None

class City(BaseModel):
    name: Optional[str] = None

class LocationInfo(BaseModel):
    id: str
    city: City

# -----------------------
# Price Models
# -----------------------
class Price(BaseModel):
    minimum_value: str
    maximum_value: str
    estimated_value: str

    def __str__(self) -> str:
        return f"Min: ₹{self.minimum_value}, Max: ₹{self.maximum_value}, Est: ₹{self.estimated_value}"
    
class Time(BaseModel):
    label: str
    timestamp: str
    
    def __str__(self) -> str:
        try:
            # Parse the ISO timestamp and extract just the date
            dt = datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
            date_str = dt.strftime('%Y-%m-%d')
            return f"{self.label}: {date_str}"
        except (ValueError, AttributeError):
            # Fallback to original timestamp if parsing fails
            return f"{self.label}: {self.timestamp}"

# -----------------------
# Item & Provider Models
# -----------------------
class Item(BaseModel):
    id: str
    descriptor: Descriptor
    location_ids: List[str]
    price: Price
    time: Optional[Time] = None

    def __str__(self) -> str:
        base_str = f"{self.descriptor.name}: {self.price}"
        if self.time:
            base_str += f" ({self.time})"
        return base_str

class Provider(BaseModel):
    id: str
    descriptor: Descriptor
    locations: List[LocationInfo]
    items: List[Item]
    time: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        lines = []
        lines.append(f"Provider: {self.descriptor.name}")
        
        if self.locations:
            lines.append("  Locations:")
            for loc in self.locations:
                lines.append(f"    - {loc.city.name}")
        
        if self.items:
            lines.append("  Items:")
            for item in self.items:
                lines.append(f"    - {item}")
        
        return "\n".join(lines)

# -----------------------
# Catalog & Message Models
# -----------------------
class Catalog(BaseModel):
    providers: List[Provider]

    def __str__(self) -> str:
        lines = []
        if self.providers:
            for provider in self.providers:
                provider_str = str(provider).replace("\n", "\n  ")
                lines.append(f"  {provider_str}")
        return "\n".join(lines)

class Message(BaseModel):
    catalog: Catalog

    def __str__(self) -> str:
        return str(self.catalog)

# -----------------------
# Context & Response Models
# -----------------------
class Context(BaseModel):
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
    location: Optional[Location] = None

class ResponseItem(BaseModel):
    context: Context
    message: Message

    def __str__(self) -> str:
        return str(self.message)

class MandiResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _has_mandi_data(self) -> bool:
        """Check if there are any responses with providers that have items."""
        for response in self.responses:
            for provider in response.message.catalog.providers:
                if provider.items and len(provider.items) > 0:
                    return True
        return False
    
    def __str__(self) -> str:
        lines = []
        lines.append("> Mandi Price Data")
        
        has_mandi_data = self._has_mandi_data()
        if not self.responses or not has_mandi_data:
            lines.append("No mandi price data found for the requested location.")
            return "\n".join(lines)
            
        lines.append("Responses:")
        for idx, rsp in enumerate(self.responses, start=1):
            rsp_str = str(rsp).replace("\n", "\n  ")
            lines.append(f"    {rsp_str}")
        return "\n".join(lines)

# -----------------------
# Commodity filtering
# -----------------------
# The API lists item names in English; farmers ask in Marathi. Map the common
# Maharashtra crop names so either form matches.
_CROP_NAME_ALIASES: Dict[str, str] = {
    "कापूस": "cotton", "सोयाबीन": "soybean", "कांदा": "onion", "टोमॅटो": "tomato",
    "बटाटा": "potato", "गहू": "wheat", "तांदूळ": "rice", "भात": "rice",
    "मका": "maize", "हरभरा": "gram", "चना": "gram", "तूर": "tur", "मूग": "moong",
    "उडीद": "urad", "भुईमूग": "groundnut", "शेंगदाणा": "groundnut", "ऊस": "sugarcane",
    "हळद": "turmeric", "डाळिंब": "pomegranate", "द्राक्षे": "grapes", "द्राक्ष": "grapes",
    "केळी": "banana", "लसूण": "garlic", "आले": "ginger", "मिरची": "chilli",
    "वांगी": "brinjal", "काकडी": "cucumber", "पेरू": "guava", "मोसंबी": "mosambi",
    "संत्रा": "orange", "ज्वारी": "sorghum", "बाजरी": "bajra", "मोहरी": "mustard",
    "कोबी": "cabbage", "फ्लॉवर": "cauliflower", "भेंडी": "ladies finger",
    "लिंबू": "lemon", "आंबा": "mango", "पपई": "papai", "कारले": "bitter gourd",
}


def _commodity_candidates(commodity: str) -> set:
    c = commodity.strip().lower()
    cands = {c}
    if c in _CROP_NAME_ALIASES:
        cands.add(_CROP_NAME_ALIASES[c])
    for mr_name, en_name in _CROP_NAME_ALIASES.items():
        if en_name in cands:
            cands.add(mr_name)
    return cands


def _name_matches(name: str, cands: set) -> bool:
    """Substring or fuzzy match, tolerant of API misspellings ('Pomegranet')."""
    from difflib import SequenceMatcher

    n = (name or "").strip().lower()
    if not n:
        return False
    for c in cands:
        if c in n or n in c:
            return True
        if SequenceMatcher(None, c, n).ratio() >= 0.75:
            return True
    return False


_MAX_UNFILTERED_ITEMS = 60
# Keep the "other commodities" list short: this is a VOICE bot — a long list
# tempts the model to recite it all, which is ~30s of TTS reading vegetable
# names (and invites garbled ad-hoc Marathi translations).
_MAX_OTHER_NAMES = 12


def render_mandi_prices(mandi_response: "MandiResponse", commodity: Optional[str]) -> str:
    """Render the catalog for the model, filtered to what the farmer asked.

    The raw catalog lists every commodity trading at the mandi (60+ items);
    dumping it into the prompt wastes tokens and buries the answer. When a
    commodity is given, return its rows plus a compact list of other names so
    the model can still offer alternatives.
    """
    rows = [
        (str(prov.descriptor) or "Mandi", item)
        for rsp in mandi_response.responses
        for prov in rsp.message.catalog.providers
        for item in prov.items
    ]
    if not rows:
        return str(mandi_response)  # keeps the standard "no data" message

    seen: set = set()
    all_names: List[str] = []
    for _, item in rows:
        n = item.descriptor.name or ""
        if n and n.lower() not in seen:
            seen.add(n.lower())
            all_names.append(n)

    if not commodity:
        lines = ["> Mandi Price Data"]
        lines += [f"- {pname}: {item}" for pname, item in rows[:_MAX_UNFILTERED_ITEMS]]
        if len(rows) > _MAX_UNFILTERED_ITEMS:
            lines.append(f"... and {len(rows) - _MAX_UNFILTERED_ITEMS} more items")
        return "\n".join(lines)

    cands = _commodity_candidates(commodity)
    matched = [(p, i) for p, i in rows if _name_matches(i.descriptor.name or i.descriptor.code or "", cands)]
    other_names = [n for n in all_names if not _name_matches(n, cands)]

    if matched:
        lines = [f"> Mandi Price Data (filtered for: {commodity})"]
        lines += [f"- {pname}: {item}" for pname, item in matched[:20]]
        if other_names:
            lines.append(
                f"Other commodities traded today (do NOT read this list aloud — mention at "
                f"most 2-3 if relevant; prices NOT shown, call mandi_prices again with "
                f"commodity=<name> to get them): {', '.join(other_names[:_MAX_OTHER_NAMES])}"
            )
        return "\n".join(lines)

    return "\n".join([
        "> Mandi Price Data",
        f"No price listed for '{commodity}' at this mandi today.",
        f"Other commodities traded today (do NOT read this list aloud — mention at most "
        f"2-3 if relevant; prices NOT shown, call mandi_prices again with "
        f"commodity=<name> to get them): {', '.join(all_names[:_MAX_OTHER_NAMES])}",
    ])


# -----------------------
# Request Model
# -----------------------
class MandiRequest(BaseModel):
    """MandiRequest model for the mandi price API.
    
    Args:
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location

    """
    latitude: float = Field(..., description="Latitude of the location")
    longitude: float = Field(..., description="Longitude of the location")
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the MandiRequest object to a dictionary.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the MandiRequest object
        """
        now = datetime.today()
        
        # start_date = now - timedelta(days=self.days_back)
        # end_date   = now
        return {
            "context": {
                "domain": "advisory:mh-vistaar",
                "action": "search",
                "location": {
                    "country": {
                        "name": "India",
                        "code": "IND"
                    }
                },
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("POCRA_BPP_ID"),
                "bpp_uri": os.getenv("POCRA_BPP_URI"),
                "message_id": str(uuid.uuid4()),
                "transaction_id": str(uuid.uuid4()),
                "timestamp": now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            },
            "message": {
                "intent": {
                    "category": {
                        "descriptor": {
                            "code": "price-discovery"
                        }
                    },
                    "item": {
                        "descriptor": {"code": ""}
                    },
                    "fulfillment": {
                        "stops": [
                            {
                                "location": {
                                    "gps": f"{self.latitude}, {self.longitude}"
                                },

                                # TODO: Add time range if needed
                                # "time": {
                                #     "range": {
                                #         "start": start_date.strftime('%Y-%m-%dT00:00:00Z'),
                                #     }
                                # }
                            }
                        ]
                    }
                }
            }
        }

async def mandi_prices(
    ctx: RunContext[FarmerContext],
    latitude: float,
    longitude: float,
    commodity: Optional[str] = None,
) -> str:
    """Get Market/Mandi prices for a specific location.

    Args:
        ctx (RunContext[FarmerContext]): The context containing session information
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location
        commodity (Optional[str]): The crop/commodity the farmer asked about, e.g.
            'cotton' or 'कापूस'. Strongly preferred — returns only that commodity's
            prices plus a short list of other traded commodities. Omit only when
            the farmer wants all prices at the mandi.

    Returns:
        str: The mandi prices for the specific location
    """
    try:
        nudge_message = get_nudge_message(
            "mandi_prices", ctx.deps.nudge_lang_code(), ctx.deps.session_id
        )
        await send_nudge_message_raya(nudge_message, ctx.deps.session_id, ctx.deps.process_id)

        # Get the mandi prices
        payload = MandiRequest(latitude=latitude, longitude=longitude).get_payload()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                os.getenv("BAP_ENDPOINT"),
                json=payload,
                timeout=15.0
            )
            
            if response.status_code != 200:
                logger.error(f"Mandi API returned status code {response.status_code}")
                return "Mandi service unavailable. Retrying"
                
            mandi_response = MandiResponse.model_validate(response.json())
            return render_mandi_prices(mandi_response, commodity)
                
    except httpx.TimeoutException as e:
        logger.error(f"Mandi API request timed out: {str(e)}")
        return "Mandi request timed out. Please try again later."
        
    except httpx.RequestError as e:
        logger.error(f"Mandi API request failed: {e}")
        return f"Mandi request failed: {str(e)}"
    
    except UnexpectedModelBehavior as e:
        logger.warning("Mandi request exceeded retry limit")
        return "Sorry, the mandi data is temporarily unavailable. Please try again later."
    
    except Exception as e:
        logger.error(f"Error getting mandi prices: {e}")
        raise ModelRetry(f"Unexpected error in mandi price request. {str(e)}")