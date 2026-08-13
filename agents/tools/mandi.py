"""
Mandi price discovery tool for fetching commodity prices from nearby mandis
using the Vistaar Beckn API.
"""
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from helpers.utils import get_logger
import httpx
import pytz
from dateutil import parser as dateutil_parser
from app.config import DEFAULT_HTTP_TIMEOUT
from pydantic import BaseModel, AnyHttpUrl, Field
from typing import List, Optional, Dict, Any
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from dotenv import load_dotenv
from pydantic_ai.tools import RunContext
from agents.deps import FarmerContext

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
# Date formatting and matching
# -----------------------
_IST = pytz.timezone("Asia/Kolkata")

# The Vistaar mandi search API only supports a from_date/to_date window of up to 30 days.
MAX_DATE_RANGE_DAYS = 30


def _parse_mandi_date(date_str: Optional[str]) -> Optional[date]:
    """Parse a mandi date string (DD-MM-YYYY or similar) to a calendar date."""
    if not date_str or not date_str.strip():
        return None
    try:
        return dateutil_parser.parse(date_str.strip(), dayfirst=True).date()
    except Exception:
        return None


def _normalize_requested_range(
    price_date: Optional[str],
    price_date_to: Optional[str],
) -> tuple[Optional[date], Optional[date], bool]:
    """Parse both ends of a requested window, swapping them when given back to front.

    Returns (start, end, is_range). is_range is True only when two *distinct* dates were
    given — a range whose ends are equal is just a single requested date.
    """
    start = _parse_mandi_date(price_date)
    end = _parse_mandi_date(price_date_to)
    if start is None or end is None:
        return start, end, False
    if start > end:
        start, end = end, start
    return start, end, start != end


def _resolve_date_range(price_date: Optional[str], price_date_to: Optional[str] = None) -> tuple[str, str]:
    """Resolve the from_date/to_date window (DD-MM-YYYY) for the mandi search payload.

    When the farmer asks for an explicit range ("01-07-2026 to 10-07-2026"), both ends are
    honoured: from_date is the start and to_date the end, capped at today (IST).

    For a single requested date, to_date stays today so the closest-available-date fallback
    still has newer arrivals to choose from; when no date was requested (latest available),
    from_date is pushed back the full window to maximize the chance of finding recent arrivals.

    Either way the window is clamped to MAX_DATE_RANGE_DAYS, since the API rejects wider ranges.
    """
    today_ist = datetime.now(_IST).date()

    start, end, _ = _normalize_requested_range(price_date, price_date_to)

    to_date = min(end, today_ist) if end is not None else today_ist
    if start is not None:
        from_date = min(start, to_date)
    else:
        # No start date: search back the full window so the latest/closest fallback has data.
        from_date = to_date - timedelta(days=MAX_DATE_RANGE_DAYS)

    from_date = max(from_date, to_date - timedelta(days=MAX_DATE_RANGE_DAYS))
    return from_date.strftime("%d-%m-%Y"), to_date.strftime("%d-%m-%Y")


def _format_price_date_display(price_date: Optional[str]) -> str:
    """Format a price or arrival date for tool output."""
    if not price_date or not price_date.strip():
        return "Latest available"
    try:
        dt = dateutil_parser.parse(price_date.strip(), dayfirst=True)
        return dt.strftime("%A, %d %B %Y")
    except Exception:
        return price_date.strip()


def _item_matches_requested_date(item: "MandiItem", requested_price_date: Optional[str]) -> bool:
    """Return True when no specific date was requested or the item's arrival date matches."""
    if not requested_price_date or not requested_price_date.strip():
        return True
    requested = _parse_mandi_date(requested_price_date)
    if requested is None:
        return True
    arrival = _parse_mandi_date(item._get_tag_value("Arrival Date"))
    return arrival is not None and arrival == requested


def _item_in_date_range(item: "MandiItem", start: date, end: date) -> bool:
    """Return True when the item's arrival date falls inside the requested range (inclusive)."""
    arrival = _parse_mandi_date(item._get_tag_value("Arrival Date"))
    return arrival is not None and start <= arrival <= end


def _display_date(day: date) -> str:
    """Format a calendar date the way it is shown in tool output."""
    return _format_price_date_display(day.strftime("%d-%m-%Y"))


def _items_on_date(items: List["MandiItem"], day: date) -> List["MandiItem"]:
    """Keep only the items whose arrival date is exactly `day`."""
    return [item for item in items if _item_matches_requested_date(item, day.strftime("%d-%m-%Y"))]


def _arrival_date_sort_key(item: "MandiItem") -> datetime:
    """Return a comparable datetime for sorting newest-first (callers sort with reverse=True);
    items without a date get the minimum datetime so they sort last."""
    arrival_date = item._get_tag_value("Arrival Date") or ""
    if not arrival_date or not arrival_date.strip():
        return datetime.min.replace(tzinfo=_IST)
    try:
        dt = dateutil_parser.parse(arrival_date.strip(), dayfirst=True)
        return dt.replace(tzinfo=_IST) if dt.tzinfo is None else dt
    except Exception:
        return datetime.min.replace(tzinfo=_IST)


def _closest_available_date(items: List["MandiItem"], target: date) -> Optional[date]:
    """Find the item arrival date closest to target; ties prefer the more recent date."""
    candidates = [
        _parse_mandi_date(item._get_tag_value("Arrival Date"))
        for item in items
    ]
    candidates = [d for d in candidates if d is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda d: (abs((d - target).days), -d.toordinal()))


# A selection is the items to display plus the header describing them, or None when the
# response holds no dated item to fall back on.
Selection = Optional[tuple[List["MandiItem"], str]]


def _closest_date_selection(items: List["MandiItem"], target: date, requested_label: str, noun: str) -> Selection:
    """Fall back to the dated items nearest `target`, labelled as a substitute for what was asked."""
    closest = _closest_available_date(items, target)
    if closest is None:
        return None
    header = (
        f"**Mandi Price Discovery** [{noun}: {requested_label} "
        f"not available — showing closest available date: {_display_date(closest)}]"
    )
    return _items_on_date(items, closest), header


def _latest_available(items: List["MandiItem"]) -> Optional[date]:
    """Return the most recent arrival date present in the response."""
    return _closest_available_date(items, datetime.now(_IST).date())


def _select_for_range(items: List["MandiItem"], start: date, end: date, requested_label: str) -> Selection:
    """Every arrival date inside the requested window, or the latest available if none match."""
    in_range = [item for item in items if _item_in_date_range(item, start, end)]
    if in_range:
        return in_range, f"**Mandi Price Discovery** [Price Date Range: {requested_label}]"
    latest = _latest_available(items)
    if latest is None:
        return None
    header = (
        f"**Mandi Price Discovery** [Requested date range: {requested_label} "
        f"not available — showing latest prices as of {_display_date(latest)}]"
    )
    return _items_on_date(items, latest), header


def _select_for_latest(items: List["MandiItem"]) -> Selection:
    """Latest available: narrow to the single most recent arrival date found."""
    latest = _latest_available(items)
    if latest is None:
        return items, "**Mandi Price Discovery** [Price Date: Latest available]"
    return _items_on_date(items, latest), f"**Mandi Price Discovery** [Price Date: {_display_date(latest)}]"


def _select_for_date(items: List["MandiItem"], requested: date, requested_label: str) -> Selection:
    """Items for the requested date, or the latest available if that date is not in the response."""
    exact_matches = _items_on_date(items, requested)
    if exact_matches:
        return exact_matches, f"**Mandi Price Discovery** [Price Date: {requested_label}]"
    latest = _latest_available(items)
    if latest is None:
        return None
    header = (
        f"**Mandi Price Discovery** [Requested date: {requested_label} "
        f"not available — showing latest prices as of {_display_date(latest)}]"
    )
    return _items_on_date(items, latest), header


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
            formatted_date = _format_price_date_display(arrival_date)
            if formatted_date:
                lines.append(f"Arrival Date: {formatted_date}")
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
            # Newest first (most recent arrival date at top)
            sorted_items = sorted(
                self.items,
                key=_arrival_date_sort_key,
                reverse=True,
            )
            for item in sorted_items:
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

    def _collect_items(self, requested_price_date: Optional[str] = None) -> List["MandiItem"]:
        """Collect items, optionally keeping only those matching the requested date."""
        items: List[MandiItem] = []
        for response in self.responses:
            for provider in response.message.catalog.providers:
                if not provider.items:
                    continue
                for item in provider.items:
                    if _item_matches_requested_date(item, requested_price_date):
                        items.append(item)
        return items

    def format_output(
        self,
        requested_price_date: Optional[str] = None,
        requested_price_date_to: Optional[str] = None,
    ) -> str:
        start, end, is_range = _normalize_requested_range(requested_price_date, requested_price_date_to)
        requested_label = (
            f"{_display_date(start)} to {_display_date(end)}"
            if is_range
            else _format_price_date_display(requested_price_date)
        )
        no_data = (
            f"**Mandi Price Discovery** [Price Date: {requested_label}]\n"
            "No mandi price data found for the requested location and commodity."
        )

        if not self.responses or not self._has_mandi_data():
            return no_data

        all_items = self._collect_items()
        if is_range:
            selection = _select_for_range(all_items, start, end, requested_label)
        elif start is None:
            selection = _select_for_latest(all_items)
        else:
            selection = _select_for_date(all_items, start, requested_label)

        if selection is None:
            return no_data

        display_items, header = selection
        lines = [header]
        for item in sorted(display_items, key=_arrival_date_sort_key, reverse=True):
            lines.append(str(item))
        return "\n---\n".join(lines)

    def __str__(self) -> str:
        return self.format_output()

# -----------------------
# Mandi Request
# -----------------------
class MandiRequest(BaseModel):
    """MandiRequest model for mandi price discovery API.

    Args:
        latitude (float): Latitude of the location, example: 26.9124
        longitude (float): Longitude of the location, example: 75.7873
        location_name (str): City or market area name, example: Jaipur
        commodity_name (str): Commodity name, example: Onion
        price_date (str): Optional price date in DD-MM-YYYY; omit only for latest available.
        price_date_to (str): Optional end of a date range in DD-MM-YYYY; price_date is the start.
    """
    latitude: float = Field(..., description="Latitude of the location")
    longitude: float = Field(..., description="Longitude of the location")
    location_name: str = Field(..., description="City or market area name")
    commodity_name: str = Field(..., description="Commodity name in English")
    price_date: Optional[str] = Field(
        default=None,
        description="Optional price date in DD-MM-YYYY; pass for today/yesterday/specific dates; omit only for latest available",
    )
    price_date_to: Optional[str] = Field(
        default=None,
        description="Optional end date in DD-MM-YYYY for a date range; pass with price_date (the start date) when the farmer asks for prices between two dates",
    )
    session_id: str = ""
    question_id: str = ""

    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the MandiRequest object to a dictionary compatible with Vistaar Beckn API.

        Returns:
            Dict[str, Any]: The dictionary representation of the request payload.
        """
        now = datetime.now(timezone.utc)
        price_date = (
            self.price_date.strip()
            if self.price_date and self.price_date.strip()
            else None
        )
        price_date_to = (
            self.price_date_to.strip()
            if self.price_date_to and self.price_date_to.strip()
            else None
        )
        from_date, to_date = _resolve_date_range(price_date, price_date_to)

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
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ttl": "PT10M",
                "location": {
                    "country": {
                        "code": "IND"
                    },
                    "city": {
                        "code": "*"
                    }
                },
                "tags": {
                    "session_id": self.session_id,
                    "question_id": self.question_id,
                },
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
                            "name": self.commodity_name
                        }
                    },
                    "fulfillment": {
                        "end": {
                            "location": {
                                "descriptor": {
                                    "name": self.location_name
                                },
                                "gps": f"{self.latitude},{self.longitude}"
                            }
                        }
                    },
                    "tags": [
                        {
                            "code": "from_date",
                            "value": from_date
                        },
                        {
                            "code": "to_date",
                            "value": to_date
                        }
                    ]
                }
            }
        }



_MAX_PAGES = 10


def _fetch_all_pages(
    search_url: str,
    base_payload: dict,
    from_date: Optional[str],
    to_date: Optional[str],
) -> Optional[List[MandiItem]]:
    """Paginate backward through the provider's 10-item cap.

    The provider returns the N most-recent arrivals within the requested window.
    We walk backward: after each call we find the oldest arrival date returned,
    set to_date = oldest - 1 day, and repeat until we reach from_date or get
    no new items.  Returns None on HTTP/network error.
    """
    all_items: List[MandiItem] = []
    seen_keys: set = set()
    current_to = to_date

    for page in range(_MAX_PAGES):
        # Always patch context with a fresh message_id so the provider doesn't
        # deduplicate repeat calls as cached responses.
        payload = {
            **base_payload,
            "context": {
                **base_payload["context"],
                "transaction_id": str(uuid.uuid4()),
                "message_id": str(uuid.uuid4()),
            },
        }
        if current_to and current_to != to_date:
            # Patch the to_date tag in the payload for subsequent pages
            tags = payload.get("message", {}).get("intent", {}).get("tags", [])
            payload = {
                **payload,
                "message": {
                    **payload["message"],
                    "intent": {
                        **payload["message"]["intent"],
                        "tags": [
                            {"code": t["code"], "value": current_to if t["code"] == "to_date" else t["value"]}
                            for t in tags
                        ],
                    },
                },
            }

        sent_tags = payload.get("message", {}).get("intent", {}).get("tags", [])
        sent_from = next((t["value"] for t in sent_tags if t.get("code") == "from_date"), None)
        sent_to = next((t["value"] for t in sent_tags if t.get("code") == "to_date"), None)
        logger.info("Mandi page %d request: from_date=%s to_date=%s", page, sent_from, sent_to)

        try:
            response = httpx.post(search_url, json=payload, timeout=DEFAULT_HTTP_TIMEOUT)
        except (httpx.TimeoutException, httpx.RequestError) as e:
            logger.error("Mandi API page %d request failed: %s", page, e)
            return None if not all_items else all_items

        if response.status_code not in (200, 201):
            logger.error("Mandi API page %d returned status %s", page, response.status_code)
            return None if not all_items else all_items

        data = response.json()
        if "message" in data and "responses" not in data:
            data = {"context": data["context"], "responses": [data]}

        try:
            parsed = MandiResponse.model_validate(data)
        except Exception as e:
            logger.error("Mandi API page %d parse error: %s", page, e)
            break

        page_items: List[MandiItem] = []
        for resp in parsed.responses:
            for provider in resp.message.catalog.providers:
                page_items.extend(provider.items)

        if not page_items:
            break

        # Deduplicate cross-page: same commodity + market + date shouldn't appear twice
        new_items = []
        for item in page_items:
            key = (item.descriptor.name, item._get_tag_value("Market"), _arrival_date_sort_key(item))
            if key not in seen_keys:
                seen_keys.add(key)
                new_items.append(item)

        all_items.extend(new_items)
        logger.info("Mandi page %d: %d items fetched, %d new", page, len(page_items), len(new_items))

        if not new_items:
            break

        # Find oldest arrival date in this page
        dated = [_parse_mandi_date(next(
            (li.value for t in item.tags for li in t.list if li.descriptor.code == "Arrival Date"), None
        )) for item in page_items]
        dated = [d for d in dated if d is not None]

        if not dated or not from_date:
            break

        oldest = min(dated)
        from_dt = _parse_mandi_date(from_date)
        if from_dt and oldest <= from_dt:
            break

        # Next page: look for arrivals before the oldest we've seen
        current_to = (oldest - timedelta(days=1)).strftime("%d-%m-%Y")

    logger.info("Mandi pagination complete: %d total items across pages", len(all_items))
    return all_items


def _build_merged_response(base_payload: dict, items: List[MandiItem]) -> MandiResponse:
    """Wrap a flat list of items into a MandiResponse for format_output()."""
    ctx_data = base_payload.get("context", {})
    context = Context(
        action=ctx_data.get("action", "search"),
        timestamp=ctx_data.get("timestamp", ""),
        message_id=ctx_data.get("message_id", ""),
        transaction_id=ctx_data.get("transaction_id", ""),
        domain=ctx_data.get("domain", ""),
        version=ctx_data.get("version", "1.1.0"),
    )
    provider = Provider(
        id="mandi-price-discovery",
        descriptor=Descriptor(name="Mandi Price Discovery"),
        items=items,
    )
    catalog = Catalog(descriptor=Descriptor(), providers=[provider])
    message = Message(catalog=catalog)
    response_item = ResponseItem(context=context, message=message)
    return MandiResponse(context=context, responses=[response_item])


async def get_mandi_prices(
    ctx: RunContext[FarmerContext],
    latitude: float,
    longitude: float,
    location_name: str,
    commodity_name: str,
    price_date: Optional[str] = None,
    price_date_to: Optional[str] = None,
) -> str:
    """Get mandi prices for a specific commodity near a location.

    Use this tool to fetch commodity price information from nearby mandis (agricultural markets).
    Use forward_geocode for coordinates and location_name (city or district from the query).
    Use search_commodity to resolve the English commodity name (the name column, e.g. Onion).

    Args:
        latitude (float): Latitude of the location
        longitude (float): Longitude of the location
        location_name (str): City or market area name (e.g. Jaipur, Pune)
        commodity_name (str): English commodity name from search_commodity (e.g. Onion)
        price_date (str): Optional price date in DD-MM-YYYY; pass for today/yesterday/specific dates.
            For a date range ("from 01-07-2026 to 10-07-2026") pass the start date here.
        price_date_to (str): Optional end date in DD-MM-YYYY; pass only for a date range, together
            with price_date as the start date (e.g. price_date=01-07-2026, price_date_to=10-07-2026).

    Returns:
        str: Formatted mandi price data for the requested commodity and location
    """
    try:
        payload = MandiRequest(
            latitude=latitude,
            longitude=longitude,
            location_name=location_name,
            commodity_name=commodity_name,
            price_date=price_date,
            price_date_to=price_date_to,
            session_id=ctx.deps.session_id,
            question_id=ctx.deps.question_id,
        ).get_payload()
        intent_tags = payload.get("message", {}).get("intent", {}).get("tags", [])
        from_date = next((t["value"] for t in intent_tags if t.get("code") == "from_date"), None)
        to_date = next((t["value"] for t in intent_tags if t.get("code") == "to_date"), None)
        logger.info(
            "Mandi request: commodity=%s location=%s (%s, %s) from=%s to=%s txn=%s",
            commodity_name, location_name, latitude, longitude,
            from_date, to_date, payload.get("context", {}).get("transaction_id"),
        )

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return "Mandi service configuration error. BAP_ENDPOINT is not set."
        search_url = bap_endpoint.rstrip("/") + "/search"
        logger.info(f"Mandi API search URL: {search_url}")

        all_items = _fetch_all_pages(search_url, payload, from_date, to_date)
        if all_items is None:
            return "Mandi service unavailable. Please try again later."

        mandi_response = _build_merged_response(payload, all_items)
        return mandi_response.format_output(
            requested_price_date=price_date,
            requested_price_date_to=price_date_to,
        )

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
