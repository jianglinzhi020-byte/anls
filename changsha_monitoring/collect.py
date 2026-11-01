"""按 15 分钟间隔采集 24 小时矩形交通态势。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from changsha_monitoring.amap import (
    AmapClient,
    AmapError,
    RoadObservation,
    dedupe_roads,
    extract_roads,
)
from changsha_monitoring.region import (
    COLLECTION_HOURS,
    ROAD_LEVEL,
    SAMPLE_MINUTES,
    ZONE_NAME,
    Tile,
    traffic_tiles,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
# 本地 Key 文件不进入 git。环境变量优先。
KEY_FILE = Path("data/changsha/amap_key")


def load_api_key() -> str:
    key = os.environ.get("AMAP_KEY") or os.environ.get("GAODE_KEY") or ""
    if not key.strip() and KEY_FILE.is_file():
        key = KEY_FILE.read_text(encoding="utf-8")
    key = key.strip().strip('"').strip("'")
    if not key:
        raise AmapError(
            "请设置环境变量 AMAP_KEY。" "该 Key 需开通交通态势服务。",
        )
    return key


def align_slot(moment: datetime, interval_min: int) -> datetime:
    local = moment.astimezone(SHANGHAI)
    minute = (local.minute // interval_min) * interval_min
    return local.replace(minute=minute, second=0, microsecond=0)


def snapshot_name(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y%m%dT%H%M%z") + ".json"


def collect_once(
    client: AmapClient,
    tiles: list[Tile],
    moment: datetime,
    out_dir: Path,
    pause_s: float = 0.25,
    sleep: Callable[[float], None] | None = None,
) -> Path:
    """请求每个矩形,写出一个时间槽的道路速度和坐标。"""
    sleeper = sleep or time.sleep
    collected_at = moment.astimezone(SHANGHAI).isoformat()
    roads: list[RoadObservation] = []
    errors: list[dict[str, str]] = []
    for index, tile in enumerate(tiles):
        try:
            payload = client.traffic_rectangle(tile)
            roads.extend(extract_roads(payload, tile.tile_id))
        except AmapError as exc:
            errors.append({"tile_id": tile.tile_id, "info": str(exc)})
        if pause_s > 0 and index < len(tiles) - 1:
            sleeper(pause_s)
    if not roads:
        detail = errors[0]["info"] if errors else "没有返回道路"
        raise AmapError(f"本次采样没有读到道路:{detail}")
    snapshot = {
        "schema": 1,
        "zone": ZONE_NAME,
        "collected_at": collected_at,
        "road_level": client.level,
        "tile_count": len(tiles),
        "errors": errors,
        "roads": [road.as_dict(collected_at) for road in dedupe_roads(roads)],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / snapshot_name(moment)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def collect_series(
    client: AmapClient,
    out_dir: Path,
    hours: float = COLLECTION_HOURS,
    interval_min: int = SAMPLE_MINUTES,
    max_tiles: int | None = None,
    pause_s: float = 0.25,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[Path]:
    """从当前时刻起连续采样。已有时间槽会跳过,中断后可以接着跑。"""
    clock = now or (lambda: datetime.now(SHANGHAI))
    sleeper = sleep or time.sleep
    tiles = traffic_tiles()
    if max_tiles is not None:
        tiles = tiles[:max_tiles]
    deadline = align_slot(clock(), interval_min) + timedelta(hours=hours)
    written: list[Path] = []
    while True:
        slot = align_slot(clock(), interval_min)
        if slot >= deadline:
            break
        path = out_dir / snapshot_name(slot)
        if path.exists():
            written.append(path)
            print(f"跳过已有 {path.name}", flush=True)
        else:
            written.append(
                collect_once(
                    client,
                    tiles,
                    slot,
                    out_dir,
                    pause_s=pause_s,
                    sleep=sleeper,
                )
            )
            print(f"已保存 {path.name}", flush=True)
        nxt = slot + timedelta(minutes=interval_min)
        if nxt >= deadline:
            break
        delay = (nxt - clock()).total_seconds()
        if delay > 0:
            sleeper(delay)
    return written


def default_client() -> AmapClient:
    return AmapClient(load_api_key(), level=ROAD_LEVEL)
