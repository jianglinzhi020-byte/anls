"""高德矩形交通态势客户端。"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from changsha_monitoring.region import ROAD_LEVEL, Tile

RECTANGLE_URL = "https://restapi.amap.com/v3/traffic/status/rectangle"
# 这些错误重试也不会成功。
_FATAL_INFOCODES = {"10001", "10002", "10003", "10009"}


class AmapError(RuntimeError):
    """高德接口拒绝或返回无法解析。"""


def _urlopen_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "changsha-monitoring/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def round_polyline(polyline: str) -> str:
    parts: list[str] = []
    for token in polyline.split(";"):
        token = token.strip()
        if not token:
            continue
        lon_text, lat_text = token.split(",")
        parts.append(f"{float(lon_text):.5f},{float(lat_text):.5f}")
    return ";".join(parts)


def road_identity(
    name: str, direction: str, lcodes: str, polyline: str
) -> str:
    if lcodes.strip():
        return lcodes.strip()
    return f"{name}|{direction}|{round_polyline(polyline)}"


@dataclass
class RoadObservation:
    name: str
    direction: str
    status: str
    angle: str
    speed_kmh: float
    lcodes: str
    polyline: str
    tile_id: str

    def identity(self) -> str:
        return road_identity(
            self.name, self.direction, self.lcodes, self.polyline
        )

    def as_dict(self, collected_at: str) -> dict[str, object]:
        return {
            "time": collected_at,
            "name": self.name,
            "direction": self.direction,
            "status": self.status,
            "angle": self.angle,
            "speed_kmh": self.speed_kmh,
            "lcodes": self.lcodes,
            "polyline": self.polyline,
            "tile_id": self.tile_id,
        }


def extract_roads(
    payload: dict[str, object], tile_id: str
) -> list[RoadObservation]:
    traffic = payload.get("trafficinfo")
    if not isinstance(traffic, dict):
        return []
    roads = traffic.get("roads")
    if not isinstance(roads, list):
        return []
    extracted: list[RoadObservation] = []
    for road in roads:
        if not isinstance(road, dict):
            continue
        observation = _observation_from_road(road, tile_id)
        if observation is not None:
            extracted.append(observation)
    return extracted


def _observation_from_road(
    road: dict[str, object], tile_id: str
) -> RoadObservation | None:
    polyline = str(road.get("polyline") or "").strip()
    if not polyline:
        return None
    try:
        speed = float(str(road.get("speed")))
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    return RoadObservation(
        name=str(road.get("name") or "").strip(),
        direction=str(road.get("direction") or "").strip(),
        status=str(road.get("status") or "").strip(),
        angle=str(road.get("angle") or "").strip(),
        speed_kmh=speed,
        lcodes=str(road.get("lcodes") or "").strip(),
        polyline=polyline,
        tile_id=tile_id,
    )


def dedupe_roads(roads: list[RoadObservation]) -> list[RoadObservation]:
    """重叠矩形里的同一路段只留一条,速度取更慢的那条。"""
    kept: dict[str, RoadObservation] = {}
    for road in roads:
        key = road.identity()
        previous = kept.get(key)
        if previous is None:
            kept[key] = road
            continue
        slower = road.speed_kmh < previous.speed_kmh
        longer_same_speed = road.speed_kmh == previous.speed_kmh and len(
            road.polyline
        ) > len(previous.polyline)
        if slower or longer_same_speed:
            kept[key] = road
    return list(kept.values())


class AmapClient:
    """只从环境变量读 Key,不把 Key 写进异常或日志。"""

    def __init__(
        self,
        key: str,
        level: int = ROAD_LEVEL,
        opener: Callable[[str, float], bytes] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not key.strip():
            raise AmapError("缺少高德 Web 服务 Key")
        self._key = key.strip()
        self.level = level
        self._opener = opener or _urlopen_bytes
        self._sleep = sleep or time.sleep

    def __repr__(self) -> str:
        return f"AmapClient(level={self.level})"

    def traffic_rectangle(self, tile: Tile) -> dict[str, object]:
        if tile.diagonal_m() >= 10_000:
            raise AmapError(f"{tile.tile_id} 对角线超过 10 公里")
        query = urllib.parse.urlencode(
            {
                "key": self._key,
                "rectangle": tile.rectangle(),
                "level": str(self.level),
                "extensions": "all",
                "output": "JSON",
            }
        )
        url = f"{RECTANGLE_URL}?{query}"
        return self._read(url)

    def _read(self, url: str) -> dict[str, object]:
        delay = 1.0
        last_error = "请求失败"
        for attempt in range(3):
            try:
                raw = self._opener(url, 20.0)
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                last_error = exc.__class__.__name__
                if attempt == 2:
                    break
                self._sleep(delay)
                delay *= 2
                continue
            if not isinstance(payload, dict):
                last_error = "返回不是 JSON 对象"
                break
            if str(payload.get("status")) == "1":
                return payload
            last_error = str(
                payload.get("info") or payload.get("infocode") or "未知错误"
            )
            if str(payload.get("infocode") or "") in _FATAL_INFOCODES:
                break
            if attempt == 2:
                break
            self._sleep(delay)
            delay *= 2
        raise AmapError(last_error)
