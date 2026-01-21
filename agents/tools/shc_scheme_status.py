import os
import re
import uuid
import base64
import hashlib
from datetime import datetime, timezone
from helpers.utils import get_logger
import httpx
from pydantic import BaseModel, AnyHttpUrl
from typing import List, Optional, Dict, Any, Literal
from app.core.cache import cache
from pydantic_ai import ModelRetry, UnexpectedModelBehavior
from dotenv import load_dotenv
from helpers.inject_pdf_header import inject
from markdownify import MarkdownConverter
load_dotenv()


class RemoveImageConverter(MarkdownConverter):
    def __init__(self, **options):
        # Prefer compact markdown: ATX headers, autolinks, '-' bullets
        options.setdefault('heading_style', 'ATX')
        options.setdefault('autolinks', True)
        options.setdefault('bullets', '-')
        super().__init__(**options)

    def convert_img(self, el, text, parent_tags):
        return ''

    def convert_hr(self, el, text, parent_tags):
        # Drop horizontal rules to save tokens
        return ''

    def convert_br(self, el, text, parent_tags):
        # Single newline for explicit breaks
        return '\n'

    def convert_p(self, el, text, parent_tags):
        # Single newline after paragraphs; later collapsed to one
        return f"{text}\n"

# Convert HTML to markdown, removing all images
def html_to_md_no_images(html_content):
    md_content = RemoveImageConverter().convert(html_content)
    # Collapse multiple blank lines to a single newline
    md_content = re.sub(r'\n{2,}', '\n', md_content)
    
    # Merge label/value blocks into single lines: supports "Key:", "Key :" or "Key" followed by values.
    lines = md_content.splitlines()
    out_lines: List[str] = []
    i = 0
    def _is_special(s: str) -> bool:
        return s.startswith('|') or s.startswith('```') or re.match(r'^\s*#{1,6}\s', s) or re.match(r'^\s*[-*]\s', s)
    while i < len(lines):
        cur_raw = lines[i]
        cur = cur_raw.strip()
        if cur == '' or _is_special(cur):
            out_lines.append(cur_raw)
            i += 1
            continue
        # Identify label lines
        m_label_colon = re.match(r'^(?P<label>[^:][^:\n]{1,120}?)\s*:\s*$', cur)
        m_label_plain = re.match(r'^(?P<label>[^:][^:\n]{1,120}?)$', cur)
        label = None
        j = i + 1
        if m_label_colon:
            label = m_label_colon.group('label')
        elif m_label_plain and j < len(lines) and lines[j].strip() != '' and not _is_special(lines[j].strip()):
            # Treat as label-without-colon if followed by a value-ish line
            label = m_label_plain.group('label')
        if label is None:
            out_lines.append(cur_raw)
            i += 1
            continue
        # Collect subsequent value lines until a stopper
        # Skip initial blank lines
        while j < len(lines) and lines[j].strip() == '':
            j += 1
        values: List[str] = []
        while j < len(lines):
            s = lines[j].strip()
            if s == '' or _is_special(s) or re.match(r'^(?P<k>[^:]{1,120})\s*:\s*$', s):
                break
            values.append(s.rstrip(','))
            j += 1
        if values:
            value_str = ', '.join(values)
            value_str = re.sub(r'\s*,\s*', ', ', value_str)
            value_str = re.sub(r'\s{2,}', ' ', value_str).strip()
            out_lines.append(f"{label}: {value_str}")
            i = j
            continue
        # Fallback if no values collected
        out_lines.append(cur_raw)
        i += 1
    return '\n'.join(out_lines)


# Constants
API_BASE_URL = os.getenv("API_BASE_URL")
FILE_TTL = 10 * 60  # 10 minutes in seconds

logger = get_logger(__name__)

# (Removed compact-output helpers; markdown conversion is used instead)

# Mimetype helpers
def _normalize_mimetype(value: Optional[str]) -> str:
    if not value:
        return ""
    return str(value).lower().split(";")[0].strip()

def _is_html_mimetype(value: Optional[str]) -> bool:
    mt = _normalize_mimetype(value)
    return "html" in mt

# Base64 HTML detection helpers
_DATA_HTML_PREFIX = "data:text/html"
_LIKELY_HTML_BASE64_PREFIXES = (
    "PCFET0NU",  # <!DOCTYPE
    "PGh0bWw",   # <html
)

_LIKELY_HTML_MARKERS = (
    "<!DOCTYPE", "<html", "<head", "<meta", "<title", "<body"
)

def _base64_pad(payload: str) -> str:
    # remove whitespace and pad to length % 4 == 0
    compact = "".join(payload.split())
    missing = (-len(compact)) % 4
    if missing:
        compact += "=" * missing
    return compact

def _safe_decode_head(payload: str, max_chars: int = 256) -> Optional[str]:
    try:
        padded = _base64_pad(payload[: max_chars])
        decoded = base64.b64decode(padded, validate=False)
        return decoded.decode("utf-8", errors="ignore")
    except Exception:
        return None

def _is_data_url_html(url: str) -> bool:
    return isinstance(url, str) and url.startswith(_DATA_HTML_PREFIX)

def _extract_base64_payload(url: str) -> str:
    if _is_data_url_html(url):
        # For data URLs like data:text/html;base64,XXXXX take substring after the first comma
        try:
            comma_idx = url.index(',')
            return url[comma_idx + 1:]
        except ValueError:
            return url[len(_DATA_HTML_PREFIX):]
    return url

def _looks_like_base64_html(url: str) -> bool:
    if not isinstance(url, str):
        return False
    payload = _extract_base64_payload(url)
    if any(payload.startswith(prefix) for prefix in _LIKELY_HTML_BASE64_PREFIXES):
        return True
    # Heuristic: attempt to decode a small head and look for HTML markers
    head = _safe_decode_head(payload)
    if not head:
        return False
    head_lower = head.lstrip()[:64].lower()
    return any(marker.lower() in head_lower for marker in _LIKELY_HTML_MARKERS)

# -----------------------
# Basic Models
# -----------------------

class Image(BaseModel):
    url: str

class Descriptor(BaseModel):
    name: Optional[str] = None
    short_desc: Optional[str] = None
    long_desc: Optional[str] = None
    images: Optional[List[Image]] = None

class ListItem(BaseModel):
    code: str
    value: str

class MediaResource(BaseModel):
    mimetype: Optional[str] = None
    url: Optional[str] = None
    injected_html: Optional[str] = None
    file_hash: Optional[str] = None
    cached_url: Optional[str] = None

    def is_html_report(self) -> bool:
        return bool(self.url) and (_is_html_mimetype(self.mimetype) or _looks_like_base64_html(self.url))

    def _decode_html(self) -> Optional[str]:
        if not self.url:
            return None
        payload = _extract_base64_payload(self.url)
        # Try full decode with padding correction; fallback to head heuristic
        try:
            padded = _base64_pad(payload)
            return base64.b64decode(padded, validate=False).decode('utf-8', errors='ignore')
        except Exception:
            head = _safe_decode_head(payload, max_chars=1024)
            return head

    def ensure_injected(self, phone_number: str, cycle: str, unique_suffix: str) -> None:
        if self.injected_html:
            return
        html_content = self._decode_html()
        if not html_content:
            return
        try:
            # Create a stable-ish file hash for this media instance
            self.file_hash = generate_file_hash(phone_number, f"{cycle}:{unique_suffix}")
            self.injected_html = inject(
                html_content,
                pdf_filename=f"soil_health_card_{self.file_hash}.pdf",
                label="Download PDF",
                selector=".container"
            )
        except Exception:
            # Fallback to original HTML if injection fails
            self.injected_html = html_content

    async def cache_and_update_url(self) -> None:
        if not self.injected_html or not self.file_hash:
            return
        cache_key = f"html_file:{self.file_hash}"
        await cache.set(cache_key, self.injected_html, ttl=FILE_TTL)
        self.cached_url = f"{API_BASE_URL}/api/file/{self.file_hash}"
        self.url = self.cached_url

    def to_markdown(self) -> str:
        # Prefer injected HTML for markdown conversion; fallback to raw decoded
        html = self.injected_html
        if not html:
            html = self._decode_html()
        if not html:
            return ""
        md = html_to_md_no_images(html)
        link = self.url or self.cached_url
        if link:
            return f"{md}\n\nOpen full report: {link}"
        return md

class Tag(BaseModel):
    list: Optional[List[ListItem]] = None
    code: Optional[str] = None
    value: Optional[str] = None

    def __str__(self) -> str:
        # Keep very light logic to avoid dumping large JSON-like values accidentally
        if self.code == "report_format":
            return ""
        if self.list:
            return ", ".join(f"{item.code}: {item.value}" for item in self.list)
        if self.code and self.value:
            if self.value.strip().startswith("{"):
                return ""
            return f"{self.code}: {self.value}"
        return ""

class TimeInfo(BaseModel):
    timestamp: str

class Time(BaseModel):
    time: TimeInfo

class Fulfillment(BaseModel):
    id: str
    type: str
    start: Time
    end: Time
    tags: List[Tag]

class Item(BaseModel):
    id: str
    descriptor: Optional[Descriptor] = None
    tags: List[Tag]
    media: Optional[List[MediaResource]] = None
    fulfillments: Optional[List[Fulfillment]] = None

    def __str__(self) -> str:
        # Prefer markdown produced from the HTML report when present
        parts: List[str] = []
        if self.descriptor and self.descriptor.name:
            parts.append(self.descriptor.name)
        if self.media:
            for media in self.media:
                if media.is_html_report():
                    md = media.to_markdown()
                    if md:
                        parts.append(md)
                        break
        return "\n\n".join(parts)

class Provider(BaseModel):
    id: str
    descriptor: Optional[Descriptor] = None
    items: List[Item]

    def __str__(self) -> str:
        lines = []
        if self.descriptor:
            if self.descriptor.name:
                lines.append(f"## Provider: {self.descriptor.name}")
            if self.descriptor.short_desc:
                lines.append(self.descriptor.short_desc)
        
        for i, item in enumerate(self.items):
            lines.append(f"\n## Report {i+1}\n{str(item)}")
        
        return "\n---\n".join(lines)

class Order(BaseModel):
    provider: Dict[str, Any]
    providers: List[Provider]
    type: str = "DEFAULT"

    def __str__(self) -> str:
        lines = []
        for provider in self.providers:
            provider_str = str(provider)
            if provider_str:
                lines.append("\n" + provider_str)
        
        return "\n".join(lines)

class Message(BaseModel):
    order: Order

    def __str__(self) -> str:
        return str(self.order)

class Location(BaseModel):
    country: Dict[str, str]

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
    location: Optional[Location] = None

class ResponseItem(BaseModel):
    context: Context
    message: Message

    def __str__(self) -> str:
        return str(self.message)

class SHCStatusResponse(BaseModel):
    context: Context
    responses: List[ResponseItem]

    def _has_valid_data(self) -> bool:
        """Check if there are any responses with valid data."""
        if not self.responses:
            return False
        
        for response in self.responses:
            if response.message.order.providers:
                for provider in response.message.order.providers:
                    if provider.items:
                        return True
        return False

    def __str__(self) -> str:
        if not self._has_valid_data():
            return "No data available."
            
        lines = []
        for response in self.responses:
            response_str = str(response)
            if response_str:
                lines.append(response_str)
        
        return "\n".join(lines).strip()

# -----------------------
# Request Models
# -----------------------

def format_phone_number(phone: str) -> str:
    """Format phone number to match the required format (+91XXXXXXXXXX).
    
    Args:
        phone (str): Input phone number in any format
        
    Returns:
        str: Formatted phone number with +91 prefix
    """
    # Remove any non-digit characters
    digits = ''.join(filter(str.isdigit, phone))
    
    # Handle different cases
    if len(digits) == 10:  # Just the number
        return f"+91{digits}"
    elif len(digits) == 12 and digits.startswith('91'):  # With 91 prefix
        return f"+{digits}"
    elif len(digits) == 11 and digits.startswith('0'):  # With 0 prefix
        return f"+91{digits[1:]}"
    else:
        raise ValueError("Invalid phone number format. Please provide a 10-digit Indian mobile number.")

class SHCStatusRequest(BaseModel):
    """SHCStatusRequest model for checking soil health card status.
    
    Args:
        phone_number (str): Phone number registered with the scheme (required)
        cycle (str): Cycle year in format YYYY-YY (e.g., "2023-24", "2024-25") (required)
    """
    phone_number: str  # Required field, no default
    cycle: str  # Required field, no default  
    
    def validate_phone_number(self) -> None:
        """Validate and format the phone number before using it."""
        try:
            self.phone_number = format_phone_number(self.phone_number)
        except ValueError as e:
            raise ValueError(str(e))
    
    def get_payload(self) -> Dict[str, Any]:
        """
        Convert the SHCStatusRequest object to a dictionary.
        
        Returns:
            Dict[str, Any]: The dictionary representation of the SHCStatusRequest object
        """
        # Validate and format phone number before generating payload
        self.validate_phone_number()
        now = datetime.today()
        
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
                "timestamp": now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            },
            "message": {
                "order": {
                    "provider": {
                        "id": "shc-discovery"
                    },
                    "items": [
                        {
                            "id": "soil-health-card"
                        }
                    ],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "tags": [
                                        {
                                            "descriptor": {
                                                "code": "cycle"
                                            },
                                            "value": self.cycle
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
# Cache Helper Functions
# -----------------------

def generate_file_hash(phone_number: str, cycle: str) -> str:
    """Generate a unique hash for the file based on phone number and cycle.
    
    Args:
        phone_number (str): The phone number (will be normalized)
        cycle (str): The cycle year
        
    Returns:
        str: A unique hash for the combination
    """
    # Normalize phone number to ensure consistent key generation
    normalized_phone = format_phone_number(phone_number)
    # Create a hash to ensure consistent key length and avoid special characters
    key_data = f"{normalized_phone}:{cycle}:{uuid.uuid4()}"  # Add UUID for uniqueness per request
    _hash = hashlib.md5(key_data.encode()).hexdigest()[:8]
    return _hash


async def cache_html_and_replace_urls(response_data: SHCStatusResponse, phone_number: str, cycle: str) -> SHCStatusResponse:
    """Find HTML media, inject PDF header, cache, and replace URLs on the model."""

    base64_url_count = 0
    for response_idx, response in enumerate(response_data.responses):
        if not response.message or not response.message.order:
            continue
        for provider_idx, provider in enumerate(response.message.order.providers):
            for item_idx, item in enumerate(provider.items):
                if not item.media:
                    continue
                for media_idx, media in enumerate(item.media):
                    if media and media.is_html_report():
                        base64_url_count += 1
                        media.ensure_injected(phone_number, cycle, f"{response_idx}-{provider_idx}-{item_idx}-{media_idx}")
                        await media.cache_and_update_url()

    logger.info(f"Processed {base64_url_count} HTML/base64 HTML media items")
    return response_data

        
    


# -----------------------
# Functions
# -----------------------

async def check_shc_status(
    phone_number: str,
    cycle: str
) -> str:
    """Check soil health card status.
    
    Use this tool to check soil health card status for a farmer.
    
    CRITICAL INSTRUCTIONS FOR USING THIS TOOL:
    - You MUST ask the user for the cycle year BEFORE calling this tool
    - NEVER assume, infer, or use a default cycle year (like "2024-25" or current year)
    - NEVER call this tool without the user explicitly providing the cycle year
    - Wait for the user's response with the cycle year before calling this function
    - The voice agent can format the year correctly, so you only need to ask naturally
    
    Args:
        phone_number (str): Phone number registered with the scheme
        cycle (str): Cycle year for which status is requested (e.g., "2023-24", "2024-25"). 
            MUST be provided by the user - never assume or infer this value.
            Format: YYYY-YY (e.g., "2023-24" means April 2023 to March 2024)
    
    Returns:
        str: Detailed soil health card information
    """
    # Validate cycle format
    if not cycle or cycle.strip() == "":
        return "I need to know which cycle year you want to check. Please provide the cycle year (for example, 2023-24 or 2024-25)."
    
    # Validate cycle format matches YYYY-YY pattern
    cycle_pattern = re.match(r'^(\d{4})-(\d{2})$', cycle.strip())
    if not cycle_pattern:
        return f"The cycle year format is incorrect. Please provide it in the format YYYY-YY (for example, 2023-24 or 2024-25). You provided: {cycle}"
    
    try:
        payload = SHCStatusRequest(
            cycle=cycle,
            phone_number=phone_number
        ).get_payload()
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                os.getenv("BAP_ENDPOINT").rstrip("/") + "/init",
                json=payload,
                timeout=httpx.Timeout(10.0, read=15.0)
            )
        
            if response.status_code != 200:
                logger.error(f"Soil health card status API returned status code {response.status_code}")
                return f"Soil health card status service unavailable. Status code: {response.status_code}"
            
            scheme_response = SHCStatusResponse.model_validate(response.json())
            
            # Cache HTML content and replace URLs
            modified_response = await cache_html_and_replace_urls(scheme_response, phone_number, cycle)
            return str(modified_response)
                
    except httpx.TimeoutException as e:
        logger.error(f"Soil health card status API request timed out: {str(e)}")
        return "Soil health card status request timed out. Please try again later."
    
    except httpx.RequestError as e:
        logger.error(f"Soil health card status API request failed: {e}")
        return f"Soil health card status request failed: {str(e)}"
    
    except UnexpectedModelBehavior as e:
        logger.warning("Soil health card status request exceeded retry limit")
        return "Soil health card status service is temporarily unavailable. Please try again later."
    except Exception as e:
        logger.error(f"Error in soil health card status: {e}")
        raise ModelRetry(f"Unexpected error in soil health card status request. {str(e)}")