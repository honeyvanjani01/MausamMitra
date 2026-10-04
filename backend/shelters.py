"""
shelters.py — nearest relief camps / shelters for a district (offline
Safe Zones tab). The frontend downloads this list once while online and
caches it in localStorage, so it stays usable with zero signal during an
actual disaster.

SHELTER_DB below is a small seed dataset for demoing. For production, swap
this for your state disaster-management department's shelter registry
(many states publish one) or NDMA's data, keyed by district.
"""
import math

from fastapi import APIRouter, Query

router = APIRouter(tags=["shelters"])

SHELTER_DB = {
    "Nagpur": [
        {"name": "Govt. Higher Secondary School", "lat": 21.1580, "lon": 79.0950, "capacity": 300,
         "phone": "+911234500001"},
        {"name": "Community Hall, Sector 4", "lat": 21.1390, "lon": 79.0700, "capacity": 150,
         "phone": "+911234500002"},
        {"name": "District Relief Camp", "lat": 21.1200, "lon": 79.1100, "capacity": 500,
         "phone": "+911234500003", "note": "medical on-site"},
    ],
}

DEFAULT_DISTRICT = "Nagpur"


def _bearing_compass(lat1, lon1, lat2, lon2) -> str:
    d_lon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(d_lon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(d_lon)
    deg = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[round(deg / 45) % 8]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@router.get("/shelters")
def get_shelters(
    district: str = Query(DEFAULT_DISTRICT),
    lat: float = Query(...),
    lon: float = Query(...),
):
    entries = SHELTER_DB.get(district, SHELTER_DB[DEFAULT_DISTRICT])

    out = []
    for s in entries:
        dist = _haversine_km(lat, lon, s["lat"], s["lon"])
        out.append(
            {
                "name": s["name"],
                "direction": _bearing_compass(lat, lon, s["lat"], s["lon"]),
                "distance_km": round(dist, 1),
                "capacity": s["capacity"],
                "phone": s["phone"],
                "note": s.get("note"),
            }
        )
    out.sort(key=lambda s: s["distance_km"])

    return {"district": district, "shelters": out}
