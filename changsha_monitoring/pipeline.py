"""把快照整理成路口、监测任务和算例。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from changsha_monitoring.amap import road_identity
from changsha_monitoring.cdi import (
    TASK_CDI,
    cdi_for_observation,
    series_baseline,
    tasks_from_samples,
)
from changsha_monitoring.collect import SHANGHAI
from changsha_monitoring.instances import make_small_instances, select_large
from changsha_monitoring.intersections import (
    SegmentGeom,
    find_intersections,
    parse_polyline_xy,
)
from changsha_monitoring.landing_pads import curated_landing_pads
from changsha_monitoring.numbers import as_float, as_int
from changsha_monitoring.region import (
    CENTER_LAT,
    CENTER_LON,
    CENTER_NAME,
    INNER_RADIUS_M,
    OUTER_RADIUS_M,
    ROAD_LEVEL,
    TARGET_INTERSECTIONS,
    TARGET_PADS,
    TARGET_TASKS,
    ZONE_NAME,
)

if TYPE_CHECKING:
    from pathlib import Path


def load_snapshots(snapshot_dir: Path) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    for path in sorted(snapshot_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(
            payload.get("roads"), list
        ):
            snapshots.append(payload)
    return snapshots


def segments_from_snapshots(
    snapshots: list[dict[str, object]],
) -> list[SegmentGeom]:
    by_key: dict[str, SegmentGeom] = {}
    for snapshot in snapshots:
        collected_at = str(snapshot.get("collected_at") or "")
        roads = snapshot.get("roads")
        if not isinstance(roads, list):
            continue
        for road in roads:
            if not isinstance(road, dict):
                continue
            name = str(road.get("name") or "").strip()
            polyline = str(road.get("polyline") or "")
            lcodes = str(road.get("lcodes") or "").strip()
            direction = str(road.get("direction") or "").strip()
            raw_speed = road.get("speed_kmh")
            if isinstance(raw_speed, bool) or not isinstance(
                raw_speed, (int, float)
            ):
                continue
            speed = float(raw_speed)
            status = str(road.get("status") or "").strip()
            key = road_identity(name, direction, lcodes, polyline)
            segment = by_key.get(key)
            points = parse_polyline_xy(polyline)
            if segment is None:
                segment = SegmentGeom(key=key, name=name, xys=points)
                by_key[key] = segment
            elif len(points) > len(segment.xys):
                segment.xys = points
            if collected_at:
                previous = segment.speeds.get(collected_at)
                if previous is None or speed < previous:
                    segment.speeds[collected_at] = speed
                    segment.statuses[collected_at] = status
    return list(by_key.values())


def _minute_of_day(stamp: str) -> int:
    moment = datetime.fromisoformat(stamp).astimezone(SHANGHAI)
    return moment.hour * 60 + moment.minute


def build_monitoring_tasks(
    intersections: list[dict[str, object]],
    segments: list[SegmentGeom],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_key = {segment.key: segment for segment in segments}
    task_rows: list[dict[str, object]] = []
    point_rows: list[dict[str, object]] = []
    for intersection in intersections:
        raw_keys = intersection.get("segment_keys")
        if not isinstance(raw_keys, list):
            continue
        keys = [str(key) for key in raw_keys]
        incident = [by_key[key] for key in keys if key in by_key]
        baselines = {
            segment.key: series_baseline(
                list(segment.speeds.values()),
                list(segment.statuses.values()),
            )
            for segment in incident
        }
        stamps = sorted(
            {stamp for segment in incident for stamp in segment.speeds}
        )
        samples: list[tuple[int, float]] = []
        for stamp in stamps:
            values: list[float] = []
            for segment in incident:
                speed = segment.speeds.get(stamp)
                if speed is None:
                    continue
                free_flow, use_ratio = baselines[segment.key]
                cdi = cdi_for_observation(
                    free_flow,
                    use_ratio,
                    speed,
                    segment.statuses.get(stamp, ""),
                )
                if cdi is not None:
                    values.append(cdi)
            if values:
                samples.append((_minute_of_day(stamp), max(values)))
        windows = tasks_from_samples(samples)
        peak = max((sample[1] for sample in samples), default=None)
        point = {
            "id": intersection["id"],
            "lon": round(as_float(intersection["lon"]), 6),
            "lat": round(as_float(intersection["lat"]), 6),
            "road_names": intersection["road_names"],
            "distance_m": round(as_float(intersection["distance_m"]), 1),
            "peak_cdi": None if peak is None else round(peak, 4),
            "task_count": len(windows),
        }
        point_rows.append(point)
        for window in windows:
            task_rows.append(window.as_dict("", str(intersection["id"])))
    task_rows.sort(
        key=lambda task: (
            str(task["intersection_id"]),
            as_int(task["ei_min"]),
        )
    )
    for index, task in enumerate(task_rows, start=1):
        task["id"] = f"T{index:04d}"
    return point_rows, task_rows


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_roads(path: Path, snapshots: list[dict[str, object]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for snapshot in snapshots:
            roads = snapshot.get("roads")
            if not isinstance(roads, list):
                continue
            for road in roads:
                if isinstance(road, dict):
                    handle.write(json.dumps(road, ensure_ascii=False) + "\n")
                    count += 1
    return count


def build_dataset(snapshot_dir: Path, out_dir: Path) -> dict[str, object]:
    """三步的后两步:由已采集快照生成监测任务和算例。"""
    snapshots = load_snapshots(snapshot_dir)
    if not snapshots:
        raise FileNotFoundError(f"没有快照:{snapshot_dir}")
    segments = segments_from_snapshots(snapshots)
    intersections = find_intersections(segments)
    points, tasks = build_monitoring_tasks(intersections, segments)
    active_points = [
        point for point in points if as_int(point["task_count"]) > 0
    ]
    chosen_ids, large_tasks = select_large(tasks)
    chosen = set(chosen_ids)
    large_points = [
        point for point in active_points if str(point["id"]) in chosen
    ]
    pads = [pad.as_dict() for pad in curated_landing_pads()]
    instances, skipped = make_small_instances(large_points, large_tasks, pads)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "instances").mkdir(exist_ok=True)
    road_count = _write_roads(out_dir / "roads.jsonl", snapshots)
    _write_json(out_dir / "intersections.json", points)
    _write_json(out_dir / "tasks.json", tasks)
    _write_json(out_dir / "landing_pads.json", pads)
    large = {
        "name": "large",
        "zone": ZONE_NAME,
        "monitoring_points": large_points,
        "tasks": large_tasks,
        "landing_pads": pads,
    }
    _write_json(out_dir / "large_instance.json", large)
    written_names: list[str] = []
    for instance in instances:
        name = str(instance["name"])
        _write_json(out_dir / "instances" / f"{name}.json", instance)
        written_names.append(name)
    target_met = (
        len(large_points) == TARGET_INTERSECTIONS
        and len(large_tasks) == TARGET_TASKS
        and len(pads) == TARGET_PADS
    )
    summary: dict[str, object] = {
        "zone": ZONE_NAME,
        "center": {
            "name": CENTER_NAME,
            "lon": CENTER_LON,
            "lat": CENTER_LAT,
        },
        "inner_radius_m": INNER_RADIUS_M,
        "outer_radius_m": OUTER_RADIUS_M,
        "road_level": ROAD_LEVEL,
        "snapshots": len(snapshots),
        "road_observations": road_count,
        "intersections_detected": len(points),
        "intersections_with_tasks": len(active_points),
        "tasks_before_cap": len(tasks),
        "large": {
            "intersections": len(large_points),
            "tasks": len(large_tasks),
            "landing_pads": len(pads),
            "target_intersections": TARGET_INTERSECTIONS,
            "target_tasks": TARGET_TASKS,
            "target_landing_pads": TARGET_PADS,
            "target_met": target_met,
        },
        "instances": written_names,
        "skipped_instances": skipped,
        "cdi": {
            "definition": "自由流速度 / 实测速度",
            "task_if_greater_than": TASK_CDI,
            "freeflow": "畅通样本用速度比,否则只认缓行及以上",
            "service_minutes": {
                "cdi < 1.8": 3,
                "1.8 <= cdi <= 2.5": 6,
                "cdi > 2.5": 9,
            },
        },
    }
    _write_json(out_dir / "summary.json", summary)
    return summary
