"""
Mandi price discovery tool for fetching commodity prices from nearby mandis
using the Vistaar Beckn API.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from helpers.utils import get_logger, get_today_date_str
import httpx
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from dotenv import load_dotenv

load_dotenv()

logger = get_logger(__name__)

# -----------------------
# Images
# -----------------------
class Image(BaseModel):
    url: AnyHttpUrl

# -----------------------
# Descriptor
# -----------------------
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

# -----------------------
# Country, City & Location
# -----------------------
class Country(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None

class City(BaseModel):
    code: Optional[str] = None

class Location(BaseModel):
    country: Optional[Country] = None
    city: Optional[City] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    gps: Optional[str] = None

# -----------------------
# Context
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

# -----------------------
# TagItem & Tag
# -----------------------
class TagItem(BaseModel):
    descriptor: Descriptor
    value: str

    def __str__(self) -> str:
        desc_name = self.descriptor.name or self.descriptor.code or "Tag"
        return f"{desc_name}: {self.value}"

class Tag(BaseModel):
    descriptor: Descriptor
    list: List[TagItem]
    display: bool = True

    def __str__(self) -> str:
        items_str = "\n      ".join(str(tag_item) for tag_item in self.list)
        return items_str

# -----------------------
# Stop & Fulfillment
# -----------------------
class Stop(BaseModel):
    location: Location

class Fulfillment(BaseModel):
    id: Optional[str] = None
    stops: Optional[List[Stop]] = None
    tracking: Optional[bool] = None

    def __str__(self) -> str:
        lines = [f"Fulfillment ID: {self.id}"]
        if self.stops:
            for stop in self.stops:
                if stop.location.lat and stop.location.lon:
                    lines.append(f"  Location: {stop.location.lat}, {stop.location.lon}")
        return "\n".join(lines)

# -----------------------
# Category
# -----------------------
class Category(BaseModel):
    id: str
    descriptor: Descriptor

    def __str__(self) -> str:
        return self.descriptor.name or self.id

# -----------------------
# MandiItem
# -----------------------
class MandiItem(BaseModel):
    id: str
    descriptor: Descriptor
    matched: bool = False
    category_ids: Optional[List[str]] = None
    fulfillment_ids: Optional[List[str]] = None
    tags: Optional[List[Tag]] = None

    def _get_tag_value(self, code: str) -> Optional[str]:
        """Extract a value from the tags list by descriptor code."""
        if not self.tags:
            return None
        for tag in self.tags:
            for item in tag.list:
                if item.descriptor.code == code:
                    return item.value
        return None

    def __str__(self) -> str:
        commodity = self._get_tag_value("Commodity") or (self.descriptor.name or self.id)
        market = self._get_tag_value("Market") or ""
        district = self._get_tag_value("District") or ""
        state = self._get_tag_value("State") or ""
        modal_price = self._get_tag_value("Modal Price") or ""
        min_price = self._get_tag_value("Min Price") or ""
        max_price = self._get_tag_value("Max Price") or ""
        price_unit = self._get_tag_value("Price Unit") or ""
        arrival_date = self._get_tag_value("Arrival Date") or ""
        variety = self._get_tag_value("Variety") or ""
        grade = self._get_tag_value("Grade") or ""

        location_parts = [p for p in [market, district, state] if p]
        location_str = ", ".join(location_parts)

        lines = []
        lines.append(f"Commodity: {commodity}")
        if location_str:
            lines.append(f"Market: {location_str}")
        if modal_price:
            price_str = f"{price_unit} {modal_price}" if price_unit else modal_price
            if min_price and max_price:
                price_str += f" (Min: {min_price}, Max: {max_price})"
            lines.append(f"Price: {price_str}")
        if arrival_date:
            lines.append(f"Arrival Date: {arrival_date}")
        extras = []
        if variety:
            extras.append(f"Variety: {variety}")
        if grade:
            extras.append(f"Grade: {grade}")
        if extras:
            lines.append(" | ".join(extras))

        return "\n".join(lines)

# -----------------------
# Provider
# -----------------------
class Provider(BaseModel):
    id: str
    descriptor: Descriptor
    categories: Optional[List[Category]] = None
    fulfillments: Optional[List[Fulfillment]] = None
    items: Optional[List[MandiItem]] = None

    def __str__(self) -> str:
        lines = []
        if self.items:
            for item in self.items:
                lines.append(str(item))
        return "\n---\n".join(lines)

# -----------------------
# Catalog
# -----------------------
class Catalog(BaseModel):
    descriptor: Descriptor
    providers: List[Provider]

    def __str__(self) -> str:
        lines = []
        for provider in self.providers:
            lines.append(str(provider))
        return "\n".join(lines)

# -----------------------
# Message & ResponseItem
# -----------------------
class Message(BaseModel):
    catalog: Catalog

    def __str__(self) -> str:
        return str(self.catalog)

class ResponseItem(BaseModel):
    context: Context
    message: Message

    def __str__(self) -> str:
        return str(self.message)

# -----------------------
# Mandi Response
# -----------------------
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
        lines.append(f"**Mandi Price Discovery** [Today's Date: {get_today_date_str()}]")

        has_mandi_data = self._has_mandi_data()
        if len(self.responses) == 0 or not has_mandi_data:
            lines.append("No mandi price data found for the requested location and commodity.")
            return "\n".join(lines)

        for rsp in self.responses:
            lines.append(str(rsp))
        return "\n".join(lines)

# -----------------------
# Mandi Request
# -----------------------
class MandiRequest(BaseModel):
    """MandiRequest model for mandi price discovery API.

    Args:
        latitude (float): Latitude of the location, example: 21.6571
        longitude (float): Longitude of the location, example: 82.1612
        commodity_code (int): AGMKT commodity code, example: 2 (Paddy)
        days_back (int): Number of days to look back from today, default 2
    """
    latitude: float = Field(..., description="Latitude of the location")
    longitude: float = Field(..., description="Longitude of the location")
    commodity_code: int = Field(..., description="AGMKT commodity code")
    days_back: int = Field(default=2, description="Number of days to look back from today")

    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the MandiRequest object to a dictionary compatible with Vistaar Beckn API.

        Returns:
            Dict[str, Any]: The dictionary representation of the request payload.
        """
        now = datetime.now(timezone.utc)
        start_date = "2025-08-01T00:00:00.000Z"
        end_date = "2025-08-01T23:59:59.999Z"

        return {
            "context": {
                "domain": "schemes:vistaar",
                "action": "search",
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("BPP_ID"),
                "bpp_uri": os.getenv("BPP_URI"),
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
                "timestamp": str(int(now.timestamp())),
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
                "intent": {
                    "category": {
                        "descriptor": {
                            "code": "price-discovery"
                        }
                    },
                    "item": {
                        "descriptor": {
                            "code": "mandi"
                        }
                    },
                    "fulfillment": {
                        "stops": [
                            {
                                "location": {
                                    "lat": str(self.latitude),
                                    "lon": str(self.longitude)
                                },
                                "time": {
                                    "range": {
                                        "start": start_date,
                                        "end": end_date
                                    }
                                },
                                "commoditycode": self.commodity_code
                            }
                        ]
                    }
                }
            }
        }


async def get_mandi_prices(
    latitude: float,
    longitude: float,
    commodity_code: int,
    days_back: int = 2,
) -> str:
    """Get mandi prices for a specific commodity near a location.

    Use this tool to fetch commodity price information from nearby mandis (agricultural markets).
    You need the commodity code (use search_commodity tool to find it) and the farmer's location.

    Args:
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location
        commodity_code (int): AGMKT commodity code (use search_commodity tool to find the code)
        days_back (int): Number of days to look back from today for price data (default 2)

    Returns:
        str: Formatted mandi price data for the requested commodity and location
    """
    try:
        payload = MandiRequest(
            latitude=latitude,
            longitude=longitude,
            commodity_code=commodity_code,
            days_back=days_back,
        ).get_payload()

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return "Mandi service configuration error. BAP_ENDPOINT is not set."
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Mandi API search URL: {search_url}")
        response = httpx.post(
            search_url,
            json=payload,
            timeout=httpx.Timeout(20.0, read=30.0),
        )
        if response.status_code != 200:
            logger.error(
                "Mandi API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return "Mandi service unavailable. Please try again later."
        logger.info("Mandi API response OK")
        data = response.json()
        mandi_response = MandiResponse.model_validate(data)
        return str(mandi_response)

    except httpx.TimeoutException:
        logger.error("Mandi API request timed out")
        return "Mandi price request timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Mandi API request failed: {e}")
        return f"Mandi price request failed: {str(e)}"
    except UnexpectedModelBehavior as e:
        logger.warning("Mandi request exceeded retry limit")
        return "Mandi price data is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error getting mandi prices: {e}")
        raise ModelRetry(f"Unexpected error in mandi price request. {str(e)}")
