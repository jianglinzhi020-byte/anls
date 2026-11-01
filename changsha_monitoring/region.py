"""长沙城郊靠近主城的环带,以及高德矩形分块。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from changsha_monitoring.geo import haversine_m, to_lonlat

# 五一广场商圈中心。城郊环带绕这个点展开,不覆盖主城核心。
CENTER_NAME = "五一广场"
CENTER_LON = 112.9768
CENTER_LAT = 28.1945
INNER_RADIUS_M = 5_000.0
OUTER_RADIUS_M = 16_500.0
ROAD_LEVEL = 4
SAMPLE_MINUTES = 15
COLLECTION_HOURS = 24
TILE_STEP_M = 6_000.0
TARGET_INTERSECTIONS = 89
TARGET_TASKS = 135
TARGET_PADS = 30
ZONE_NAME = "长沙城郊靠近主城"


@dataclass(frozen=True)
class Tile:
    """高德矩形交通态势的一块查询范围。"""

    tile_id: str
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def rectangle(self) -> str:
        """左下、右上,经度在前。"""
        return (
            f"{self.min_lon:.6f},{self.min_lat:.6f};"
            f"{self.max_lon:.6f},{self.max_lat:.6f}"
        )

    def diagonal_m(self) -> float:
        return haversine_m(
            self.min_lon, self.min_lat, self.max_lon, self.max_lat
        )


def in_ring(lon: float, lat: float) -> bool:
    distance = haversine_m(CENTER_LON, CENTER_LAT, lon, lat)
    return INNER_RADIUS_M <= distance <= OUTER_RADIUS_M


def _rect_hits_ring(
    east0: float,
    north0: float,
    east1: float,
    north1: float,
) -> bool:
    """轴对齐矩形是否与环带相交。坐标以市中心为原点,单位米。"""
    closest_east = (
        0.0 if east0 <= 0.0 <= east1 else min(abs(east0), abs(east1))
    )
    closest_north = (
        0.0 if north0 <= 0.0 <= north1 else min(abs(north0), abs(north1))
    )
    closest = math.hypot(closest_east, closest_north)
    farthest = max(
        math.hypot(east, north)
        for east in (east0, east1)
        for north in (north0, north1)
    )
    return closest <= OUTER_RADIUS_M and farthest >= INNER_RADIUS_M


def traffic_tiles(step_m: float = TILE_STEP_M) -> list[Tile]:
    """把环带切成对角线短于 10 公里的矩形。

    高德矩形交通态势要求对角线不超过 10 公里。6 公里见方的
    对角线约 8.5 公里,留出余量。
    """
    tiles: list[Tile] = []
    east = -OUTER_RADIUS_M
    row = 0
    while east < OUTER_RADIUS_M:
        north = -OUTER_RADIUS_M
        col = 0
        while north < OUTER_RADIUS_M:
            east1 = east + step_m
            north1 = north + step_m
            if _rect_hits_ring(east, north, east1, north1):
                lon0, lat0 = to_lonlat(east, north, CENTER_LON, CENTER_LAT)
                lon1, lat1 = to_lonlat(east1, north1, CENTER_LON, CENTER_LAT)
                tile = Tile(
                    tile_id=f"R{row:02d}C{col:02d}",
                    min_lon=min(lon0, lon1),
                    min_lat=min(lat0, lat1),
                    max_lon=max(lon0, lon1),
                    max_lat=max(lat0, lat1),
                )
                if tile.diagonal_m() >= 10_000:
                    raise ValueError(f"{tile.tile_id} 对角线超过 10 公里")
                tiles.append(tile)
            north = north1
            col += 1
        east = east1
        row += 1
    return tiles
