from datetime import datetime
import json
import math
import urllib.request


RML_DEPOT_LAT = 13.9411


RML_DEPOT_LNG = 121.1631


RML_DEPOT_NAME = "RML Lipa City Central Depot"


RML_DEPOT_ADDRESS = "Ayala Highway, Lipa City, Batangas"


BATANGAS_GEO_CACHE = {
    "summer hills": (13.9535, 121.1550),
    "rosewood": (13.9535, 121.1550),
    "marawoy": (13.9622, 121.1624),
    "tambo": (13.9318, 121.1448),
    "sabang": (13.9385, 121.1712),
    "inosloban": (13.9782, 121.1741),
    "san carlos": (13.9560, 121.1850),
    "antipolo del norte": (13.9480, 121.1750),
    "lodlod": (13.9320, 121.1580),
    "mataas na kahoy": (13.9630, 121.1120),
    "malvar": (14.0450, 121.1580),
    "tanauan": (14.0864, 121.1528),
    "santo tomas": (14.1082, 121.1415),
    "san jose": (13.8820, 121.1080),
    "batangas city": (13.7565, 121.0583),
}


def haversine_distance_km(lat1, lon1, lat2, lon2):
    """Calculate great-circle distance between two points in kilometers."""
    try:
        r = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2.0) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(r * c, 2)
    except Exception:
        return 4.5


def resolve_delivery_coords(address, lat=None, lng=None):
    """Resolve latitude and longitude for a delivery address, prioritizing valid coordinates."""
    try:
        if lat is not None and lng is not None:
            lat_f = float(lat)
            lng_f = float(lng)
            if -90 <= lat_f <= 90 and -180 <= lng_f <= 180 and not (lat_f == 0 and lng_f == 0):
                return round(lat_f, 5), round(lng_f, 5)
    except (ValueError, TypeError):
        pass

    if not address:
        return 13.9535, 121.1550

    addr_lower = str(address).lower()
    for key, (clat, clng) in BATANGAS_GEO_CACHE.items():
        if key in addr_lower:
            return clat, clng

    # Deterministic pseudo-random offset within 2-4 km of Lipa Central Depot
    h = sum(ord(c) for c in addr_lower)
    offset_lat = ((h % 30) - 15) * 0.0012
    offset_lng = (((h // 30) % 30) - 15) * 0.0012
    return round(RML_DEPOT_LAT + 0.012 + offset_lat, 5), round(RML_DEPOT_LNG + 0.006 + offset_lng, 5)


def is_gps_stale(last_update_str, status):
    """Return True if GPS has not updated within 120 seconds for an active transit delivery."""
    if not last_update_str:
        return True
    if status not in ("en_route", "in_transit", "loading"):
        return False
    try:
        last_dt = datetime.strptime(last_update_str[:19], "%Y-%m-%d %H:%M:%S")
        diff = (datetime.now() - last_dt).total_seconds()
        return diff > 120
    except Exception:
        return False


def fetch_osrm_route(start_lat, start_lng, dest_lat, dest_lng):
    """
    Call public OSRM driving service to obtain road route polyline, distance, and duration.
    Falls back gracefully to interpolated road waypoints if OSRM is unreachable.
    """
    osrm_url = f"https://router.project-osrm.org/route/v1/driving/{start_lng},{start_lat};{dest_lng},{dest_lat}?overview=full&geometries=geojson"
    try:
        req = urllib.request.Request(
            osrm_url,
            headers={"User-Agent": "RMLConstructionSupply-Logistics/1.0 (Batangas, Philippines)"}
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]
                dist_meters = float(route.get("distance", 0))
                dur_seconds = float(route.get("duration", 0))
                geojson_coords = route.get("geometry", {}).get("coordinates", [])
                leaflet_coords = [[round(pt[1], 6), round(pt[0], 6)] for pt in geojson_coords]
                return {
                    "ok": True,
                    "provider": "osrm",
                    "distance_km": round(dist_meters / 1000.0, 2),
                    "duration_min": max(1, round(dur_seconds / 60.0)),
                    "coordinates": leaflet_coords,
                }
    except Exception:
        pass

    # Fallback interpolation
    dist_km = haversine_distance_km(start_lat, start_lng, dest_lat, dest_lng)
    est_duration_min = max(2, round(dist_km * 2.2))
    steps = 16
    fallback_coords = []
    for i in range(steps + 1):
        ratio = i / float(steps)
        curve_offset = math.sin(ratio * math.pi) * 0.0022
        w_lat = start_lat + (dest_lat - start_lat) * ratio + curve_offset
        w_lng = start_lng + (dest_lng - start_lng) * ratio - (curve_offset * 0.7)
        fallback_coords.append([round(w_lat, 6), round(w_lng, 6)])

    return {
        "ok": True,
        "provider": "fallback_interpolated",
        "distance_km": dist_km,
        "duration_min": est_duration_min,
        "coordinates": fallback_coords,
    }

