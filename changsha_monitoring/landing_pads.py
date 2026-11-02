"""30 个手工选定的城郊起降点候选位置。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from changsha_monitoring.geo import haversine_m, to_xy
from changsha_monitoring.region import (
    CENTER_LAT,
    CENTER_LON,
    TARGET_PADS,
    in_ring,
)

# 坐标来自 OpenStreetMap / Nominatim,检索日期 2026-11-02。
# 点位分布在五一广场外约 5-16.5 公里,覆盖岳麓、望城、开福、
# 长沙县、雨花和天心靠近主城的一侧。发布前可以直接改这张表。
CURATED: tuple[tuple[str, float, float], ...] = (
    ("观沙岭", 112.947079, 28.234053),
    ("树木岭", 113.015054, 28.159437),
    ("滨江新城", 112.952957, 28.243759),
    ("望城坡", 112.916501, 28.203421),
    ("岳麓山大学城", 112.925123, 28.156935),
    ("中南林业科技大学", 112.985938, 28.133427),
    ("长沙汽车西站", 112.907418, 28.213020),
    ("东岸街道", 113.050274, 28.201691),
    ("黎托街道", 113.053655, 28.171232),
    ("麓谷公园", 112.891627, 28.217192),
    ("湖南省政府", 112.981973, 28.114489),
    ("洋湖湿地公园", 112.918897, 28.129663),
    ("湖南农业大学", 113.078373, 28.184341),
    ("梅溪湖", 112.872135, 28.188513),
    ("长沙汽车南站", 113.009285, 28.100475),
    ("星沙", 113.080124, 28.248757),
    ("洞井街道", 113.032630, 28.100133),
    ("泉塘街道", 113.098004, 28.224933),
    ("月亮岛", 112.921031, 28.294720),
    ("捞刀河", 112.970791, 28.308145),
    ("中信广场", 112.983998, 28.077022),
    ("松雅湖", 113.097890, 28.248583),
    ("学士街道", 112.866477, 28.124461),
    ("雷锋街道", 112.841302, 28.205602),
    ("榔梨街道", 113.112447, 28.180137),
    ("坪塘街道", 112.906437, 28.089617),
    ("尚双塘", 112.986749, 28.065262),
    ("含浦街道", 112.851230, 28.103300),
    ("沙坪街道", 112.994440, 28.338389),
    ("青竹湖街道", 112.959366, 28.339892),
)


@dataclass(frozen=True)
class LandingPad:
    pad_id: str
    name: str
    lon: float
    lat: float

    def distance_m(self) -> float:
        return haversine_m(CENTER_LON, CENTER_LAT, self.lon, self.lat)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.pad_id,
            "name": self.name,
            "lon": self.lon,
            "lat": self.lat,
            "distance_m": round(self.distance_m(), 1),
            "source": "手工选定,坐标来自 OpenStreetMap",
        }


def _bearing(lon: float, lat: float) -> float:
    east, north = to_xy(lon, lat, CENTER_LON, CENTER_LAT)
    return math.atan2(east, north)


def curated_landing_pads() -> list[LandingPad]:
    """按绕市中心的方位角编号,返回 30 个起降点。"""
    if len(CURATED) != TARGET_PADS:
        raise ValueError(f"起降点应为 {TARGET_PADS} 个,实际 {len(CURATED)} 个")
    ordered = sorted(
        CURATED, key=lambda item: (_bearing(item[1], item[2]), item[0])
    )
    pads: list[LandingPad] = []
    for index, (name, lon, lat) in enumerate(ordered, start=1):
        pad = LandingPad(f"LP{index:02d}", name, lon, lat)
        if not in_ring(pad.lon, pad.lat):
            raise ValueError(f"{name} 不在城郊环带内")
        pads.append(pad)
    return pads
