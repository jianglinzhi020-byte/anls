"""按 15 分钟间隔采集路况,圆形矩阵默认每天不超过 100 次。"""

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
    QuotaExceeded,
    RoadObservation,
    dedupe_roads,
    extract_roads,
    is_daily_limit,
)
from changsha_monitoring.quota import QUOTA_FILE, DailyQuota
from changsha_monitoring.region import (
    COLLECTION_HOURS,
    DAILY_CALL_LIMIT,
    ROAD_LEVEL,
    SAMPLE_MINUTES,
    ZONE_NAME,
    Circle,
    Tile,
    traffic_circles,
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


def circle_for_slot(
    moment: datetime,
    circles: list[Circle],
    interval_min: int = SAMPLE_MINUTES,
) -> Circle:
    """按上海时间的 15 分钟槽轮流选圆。同一圆大约每 2 小时一次。"""
    if not circles:
        raise AmapError("圆形矩阵是空的")
    local = moment.astimezone(SHANGHAI)
    slot = (local.hour * 60 + local.minute) // interval_min
    return circles[slot % len(circles)]


def _write_snapshot(
    moment: datetime,
    out_dir: Path,
    roads: list[RoadObservation],
    errors: list[dict[str, str]],
    query: dict[str, object],
) -> Path:
    collected_at = moment.astimezone(SHANGHAI).isoformat()
    snapshot = {
        "schema": 1,
        "zone": ZONE_NAME,
        "collected_at": collected_at,
        "road_level": ROAD_LEVEL,
        "query": query,
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
    roads: list[RoadObservation] = []
    errors: list[dict[str, str]] = []
    for index, tile in enumerate(tiles):
        try:
            payload = client.traffic_rectangle(tile)
            roads.extend(extract_roads(payload, tile.tile_id))
        except QuotaExceeded:
            raise
        except AmapError as exc:
            errors.append({"tile_id": tile.tile_id, "info": str(exc)})
        if pause_s > 0 and index < len(tiles) - 1:
            sleeper(pause_s)
    if not roads:
        detail = errors[0]["info"] if errors else "没有返回道路"
        raise AmapError(f"本次采样没有读到道路:{detail}")
    return _write_snapshot(
        moment,
        out_dir,
        roads,
        errors,
        {"shape": "rectangle", "tile_count": len(tiles)},
    )


def collect_circle_once(
    client: AmapClient,
    circle: Circle,
    moment: datetime,
    out_dir: Path,
) -> Path:
    """请求一个圆,并写下这一拍的道路。空结果也保存。"""
    roads: list[RoadObservation] = []
    errors: list[dict[str, str]] = []
    try:
        payload = client.traffic_circle(circle)
        roads.extend(extract_roads(payload, circle.circle_id))
    except QuotaExceeded:
        raise
    except AmapError as exc:
        if is_daily_limit(str(exc)):
            raise QuotaExceeded(str(exc)) from exc
        errors.append({"tile_id": circle.circle_id, "info": str(exc)})
    return _write_snapshot(
        moment,
        out_dir,
        roads,
        errors,
        {
            "shape": "circle",
            "circle_id": circle.circle_id,
            "name": circle.name,
            "location": circle.location(),
            "radius_m": circle.radius_m,
        },
    )


def collect_circle_series(
    client: AmapClient,
    out_dir: Path,
    hours: float = COLLECTION_HOURS,
    interval_min: int = SAMPLE_MINUTES,
    quota: DailyQuota | None = None,
    circles: list[Circle] | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[list[Path], str]:
    """每拍只请求 1 个圆。额度用尽后停止,不再发请求。"""
    clock = now or (lambda: datetime.now(SHANGHAI))
    sleeper = sleep or time.sleep
    ring = circles if circles is not None else traffic_circles()
    ledger = quota or DailyQuota(QUOTA_FILE, DAILY_CALL_LIMIT)
    client.bind_before_request(ledger.reserve)
    deadline = align_slot(clock(), interval_min) + timedelta(hours=hours)
    written: list[Path] = []
    reason = ""
    while True:
        slot = align_slot(clock(), interval_min)
        if slot >= deadline:
            break
        path = out_dir / snapshot_name(slot)
        if path.exists():
            written.append(path)
            print(f"跳过已有 {path.name}", flush=True)
        else:
            circle = circle_for_slot(slot, ring, interval_min)
            try:
                written.append(
                    collect_circle_once(client, circle, slot, out_dir)
                )
            except QuotaExceeded as exc:
                reason = str(exc)
                if is_daily_limit(reason):
                    ledger.exhaust()
                print(reason, flush=True)
                break
            print(
                f"已保存 {path.name} {circle.circle_id}{circle.name}",
                flush=True,
            )
        nxt = slot + timedelta(minutes=interval_min)
        if nxt >= deadline:
            break
        delay = (nxt - clock()).total_seconds()
        if delay > 0:
            sleeper(delay)
    return written, reason


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
