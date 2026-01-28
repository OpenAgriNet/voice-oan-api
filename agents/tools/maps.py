"""
Maps tool for geocoding using Photon.
"""
import os
import httpx
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator
from helpers.utils import get_logger

logger = get_logger(__name__)

load_dotenv()

# Photon configuration
# PHOTON_HOST should be in format: http://host:port/api
from urllib.parse import urlparse

photon_url = os.getenv("PHOTON_HOST")
parsed = urlparse(photon_url)
PHOTON_HOST = parsed.hostname or "10.128.188.19"
PHOTON_PORT = parsed.port or 2322
PHOTON_BASE_URL = f"http://{PHOTON_HOST}:{PHOTON_PORT}"

logger.info(f"Using Photon geocoder at {PHOTON_HOST}:{PHOTON_PORT}")

class Location(BaseModel):
    """Location model for the maps tool."""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    place_name: Optional[str] = None

    @field_validator('latitude', 'longitude')
    @classmethod
    def round_coordinates(cls, v):
        if v is not None:
            return round(float(v), 3)
        return v
    
    def model_post_init(self, __context__: Any) -> None:
        """Called after the model is initialized."""
        super().model_post_init(__context__)
        # Note: Auto-reverse geocoding is skipped in model init (async not available)
        # Users should call reverse_geocode() explicitly if place_name is needed
    
    def _location_string(self):
        if self.latitude and self.longitude:
            return f"{self.place_name} (Latitude: {self.latitude}, Longitude: {self.longitude})"
        else:
            return "Location not available"

    def __str__(self):
        return f"{self.place_name} ({self.latitude}, {self.longitude})"


async def _photon_forward_geocode(place_name: str) -> Optional[List[Dict[str, Any]]]:
    """Forward geocoding using Photon API."""
    try:
        url = f"{PHOTON_BASE_URL}/api"
        params = {
            "q": place_name,
            "limit": 10,
            "lang": "en"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("features", [])
    except Exception as e:
        logger.error(f"Photon forward geocoding error for '{place_name}': {e}")
        return None


async def _photon_reverse_geocode(latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
    """Reverse geocoding using Photon API."""
    try:
        url = f"{PHOTON_BASE_URL}/reverse"
        params = {
            "lat": latitude,
            "lon": longitude,
            "lang": "en"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            feature = data.get("features", [])
            if feature:
                return feature[0]
            return None
    except Exception as e:
        logger.error(f"Photon reverse geocoding error for ({latitude}, {longitude}): {e}")
        return None


def _photon_to_location(feature: Dict[str, Any], place_name: str = None) -> Location:
    """Convert Photon feature to Location object."""
    props = feature.get("properties", {})
    geometry = feature.get("geometry", {})
    coords = geometry.get("coordinates", [])
    
    # Photon returns [lon, lat] format
    longitude = coords[0] if len(coords) > 0 else None
    latitude = coords[1] if len(coords) > 1 else None
    
    # Build display name from Photon properties
    # Format: Name, City/County, State, Country
    name_parts = []
    if props.get("name"):
        name_parts.append(props["name"])
    
    # Add city if available and different from name
    city = props.get("city")
    if city and city != props.get("name"):
        name_parts.append(city)
    elif props.get("county") and props.get("county") != props.get("name"):
        name_parts.append(props["county"])
    
    # Add state if available
    if props.get("state"):
        name_parts.append(props["state"])
    
    # Add country if available
    if props.get("country"):
        name_parts.append(props["country"])
    
    display_name = ", ".join(name_parts) if name_parts else (place_name or "Unknown Location")
    
    return Location(
        place_name=display_name,
        latitude=latitude,
        longitude=longitude
    )


async def forward_geocode(place_name: str) -> Optional[Location]:
    """Forward Geocoding to get latitude and longitude from place name.

    Args:
        place_name (str): The place name to geocode, in English.

    Returns:
        Location: The location of the place, or None if not found.
    """
    try:
        features = await _photon_forward_geocode(place_name)
        if features:
            # India bounding box coordinates [min_lat, min_lon, max_lat, max_lon]
            # Approximate coordinates for India
            india_bbox = [6.0, 68.0, 36.0, 98.0]
            
            # Filter results to India region
            for feature in features:
                geometry = feature.get("geometry", {})
                coords = geometry.get("coordinates", [])
                if len(coords) >= 2:
                    lon = coords[0]
                    lat = coords[1]
                    
                    # Check if coordinates are within India bounding box
                    if (india_bbox[0] <= lat <= india_bbox[2] and 
                        india_bbox[1] <= lon <= india_bbox[3]):
                        return _photon_to_location(feature, place_name)
            
            # If no India-specific result found, return first result anyway
            if len(features) > 0:
                return _photon_to_location(features[0], place_name)
        else:
            logger.info(f"No results found for place: {place_name}")
    except Exception as e:
        logger.error(f"Forward geocoding error for '{place_name}': {e}")
    return None


async def reverse_geocode(latitude: float, longitude: float) -> Optional[Location]:
    """Reverse Geocoding to get place name from latitude and longitude.

    Args:
        latitude (float): The latitude of the location.
        longitude (float): The longitude of the location.

    Returns:
        Location: The location of the place, or None if not found.
    """
    try:
        feature = await _photon_reverse_geocode(latitude, longitude)
        if feature:
            location = _photon_to_location(feature)
            location.latitude = latitude
            location.longitude = longitude
            return location
        else:
            logger.info(f"No results found for coordinates: ({latitude}, {longitude})")
    except Exception as e:
        logger.error(f"Reverse geocoding error for ({latitude}, {longitude}): {e}")
    return None
