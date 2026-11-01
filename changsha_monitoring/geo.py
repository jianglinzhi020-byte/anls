"""局部平面投影与球面距离。"""

from __future__ import annotations

import math

LAT_M_PER_DEG = 110540.0
LON_M_PER_DEG_EQUATOR = 111320.0
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """返回两点间的大圆距离,单位米。"""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    hav = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(hav))


def to_xy(
    lon: float, lat: float, lon0: float, lat0: float
) -> tuple[float, float]:
    """以 (lon0, lat0) 为原点,返回东、北方向的米制坐标。"""
    east = (lon - lon0) * LON_M_PER_DEG_EQUATOR * math.cos(math.radians(lat0))
    north = (lat - lat0) * LAT_M_PER_DEG
    return east, north


def to_lonlat(
    east: float, north: float, lon0: float, lat0: float
) -> tuple[float, float]:
    """把局部米制坐标变回经纬度。"""
    lat = lat0 + north / LAT_M_PER_DEG
    lon = lon0 + east / (LON_M_PER_DEG_EQUATOR * math.cos(math.radians(lat0)))
    return lon, lat
