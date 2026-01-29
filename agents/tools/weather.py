"""
Weather tool for fetching weather forecast data using the IMD API.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from helpers.utils import get_logger, get_today_date_str
import httpx
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any, Tuple
from dateutil import parser
from dateutil.parser import ParserError
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

    def is_date(self) -> Tuple[bool, Optional[datetime]]:
        """Check if the descriptor code or name contains a parseable date.
        
        Returns:
            Tuple[bool, Optional[datetime]]: (True, datetime_obj) if date found, (False, None) if not
        """
        try:
            # Try code first as it's more likely to contain the date
            if self.code:
                return True, parser.parse(self.code, fuzzy=True)
            # Try name if code didn't work
            if self.name:
                return True, parser.parse(self.name, fuzzy=True)
            return False, None
        except (ParserError, TypeError, ValueError):
            return False, None

    def __str__(self) -> str:
        """Return the 'name' or 'code' if present, else empty."""
        if self.name:
            return self.name
        elif self.code:
            return self.code
        return ""

# -----------------------
# Country & Location
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
    # Mark optional if not always present
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

    def __str__(self) -> str:
        """Example format:
           TagGroupName:
               TagItem1
               TagItem2
        """
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

    def __str__(self) -> str:
        lines = [f"Fulfillment ID: {self.id}"]
        if self.stops:
            lines.append("  Stops:")
            for stop in self.stops:
                if stop.location.gps:
                    lines.append(f"    - GPS: {stop.location.gps}")
                elif stop.location.lat and stop.location.lon:
                    lines.append(f"    - Lat: {stop.location.lat}, Lon: {stop.location.lon}")
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
# Item
# -----------------------
class Item(BaseModel):
    id: str
    descriptor: Descriptor
    matched: bool
    recommended: bool
    category_ids: Optional[List[str]] = None
    fulfillment_ids: Optional[List[str]] = None
    tags: Optional[List[Tag]] = None

    def __str__(self) -> str:
        lines = []
        # Item name / ID heading
        lines.append(f"**Item:** {self.descriptor.name or self.id}")

        # Short/Long
        if self.descriptor.short_desc:
            lines.append(f"  Short: {self.descriptor.short_desc}")
        if self.descriptor.long_desc:
            # strip() to remove trailing newlines
            lines.append(f"  Long: {self.descriptor.long_desc.strip()}")

        # Show tags
        if self.tags:
            lines.append("  Tags:")
            for t in self.tags:
                tag_str = str(t).replace("\n", "\n    ")
                lines.append(f"    {tag_str}")

        return "\n".join(lines)

# -----------------------
# Provider
# -----------------------
class Provider(BaseModel):
    id: str
    descriptor: Descriptor
    categories: Optional[List[Category]] = None
    fulfillments: Optional[List[Fulfillment]] = None
    items: Optional[List[Item]] = None

    def __str__(self) -> str:
        lines = []
        lines.append(f"Provider: {self.descriptor.name or self.id}")

        if self.categories:
            lines.append("  Categories:")
            for cat in self.categories:
                lines.append(f"    - {cat}")

        if self.fulfillments:
            lines.append("  Fulfillments:")
            for f in self.fulfillments:
                f_str = str(f).replace("\n", "\n    ")
                lines.append(f"    {f_str}")

        if self.items:
            lines.append("  Items:")
            for item in self.items:
                item_str = str(item).replace("\n", "\n    ")
                lines.append(f"    {item_str}")

        return "\n".join(lines)

# -----------------------
# Catalog
# -----------------------
class Catalog(BaseModel):
    descriptor: Descriptor
    providers: List[Provider]

    def __str__(self) -> str:
        lines = []
        lines.append(f"Catalog: {self.descriptor.name or 'N/A'}")
        if self.providers:
            lines.append("Providers:")
            for provider in self.providers:
                provider_str = str(provider).replace("\n", "\n  ")
                lines.append(f"  {provider_str}")
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
# Weather Response
# -----------------------
class WeatherResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _has_weather_data(self) -> bool:
        """Check if there are any responses with providers that have items."""
        for response in self.responses:
            for provider in response.message.catalog.providers:
                if provider.items and len(provider.items) > 0:
                    return True
        return False
    
    def __str__(self) -> str:
        lines = []
        
        lines.append(f"**Weather Forecast Data** [Today's Date: {get_today_date_str()}]")
        no_data_message = "No weather forecast data found for the requested location."
    
        # Check if there are any responses with providers that have items
        has_weather_data = self._has_weather_data()
        if len(self.responses) == 0 or not has_weather_data:
            lines.append(no_data_message)
            return "\n".join(lines)
        else:
            lines.append("Responses:")
            for idx, rsp in enumerate(self.responses, start=1):
                rsp_str = str(rsp).replace("\n", "\n  ")
                lines.append(f"    {rsp_str}")
            return "\n".join(lines)

# -----------------------
# Weather Request
# -----------------------
class WeatherRequest(BaseModel):
    """WeatherRequest model for weather forecast API.
    
    Args:
        latitude (float): Latitude of the location, example: 12.9716
        longitude (float): Longitude of the location, example: 77.5946
    """
    latitude: float = Field(..., description="Latitude of the location")
    longitude: float = Field(..., description="Longitude of the location")
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the WeatherRequest object to a dictionary compatible with Vistaar Beckn API.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the request payload.
        """
        now = datetime.now(timezone.utc)
        
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
                            "name": "Weather-Forecast",
                            "code": "WFC"
                        }
                    },
                    "fulfillment": {
                        "stops": [
                            {
                                "location": {
                                    "lat": self.latitude,
                                    "lon": self.longitude
                                }
                            }
                        ]
                    }
                }
            }
        }


async def weather_forecast(latitude: float, longitude: float) -> str:
    """Get Weather forecast for a specific location.

    Args:
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location
    
    Returns:
        str: The weather forecast for the specific location
    """    
    try:        
        payload = WeatherRequest(latitude=latitude, longitude=longitude).get_payload()
        
        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return "Weather service configuration error. BAP_ENDPOINT is not set."
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Weather API search URL: {search_url}")
        response = httpx.post(
            search_url,
            json=payload,
            timeout=httpx.Timeout(20.0, read=30.0)
        )
        if response.status_code != 200:
            logger.error(
                "Weather API returned status %s for URL %s — response: %s",
                response.status_code,
                search_url,
                response.text[:500] if response.text else "(empty)",
            )
            return "Weather service unavailable. Please try again later."
        logger.info("Weather API response OK")
        data = response.json()
        weather_response = WeatherResponse.model_validate(data)
        return str(weather_response)
                
    except httpx.TimeoutException:
        logger.error("Weather API request timed out")
        return "Weather request timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Weather API request failed: {e}")
        return f"Weather request failed: {str(e)}"
    except UnexpectedModelBehavior as e:
        logger.warning("Weather request exceeded retry limit")
        return "Weather data is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error getting weather forecast: {e}")
        raise ModelRetry(f"Unexpected error in weather forecast. {str(e)}")
