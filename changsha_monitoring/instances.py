"""大规模筛选,以及按比例分层抽取的小规模算例。"""

from __future__ import annotations

import hashlib
import math
import random
from typing import Sequence

from changsha_monitoring.numbers import as_float, as_int
from changsha_monitoring.region import TARGET_INTERSECTIONS, TARGET_TASKS

# 与论文小规模对照表一致:监测点数 3-10,起降点数 2-5。
SMALL_SERIES: tuple[tuple[int, int], ...] = (
    (3, 2),
    (5, 3),
    (7, 4),
    (10, 5),
)
REPLICATES = 5


def select_large(
    tasks: Sequence[dict[str, object]],
    intersection_limit: int = TARGET_INTERSECTIONS,
    task_limit: int = TARGET_TASKS,
) -> tuple[list[str], list[dict[str, object]]]:
    """保留拥堵最重的路口和任务。

    先给每个路口留下 CDI 最高的一条任务,再按 CDI 补满任务数。
    数据不够时返回实际数量,不编造路口。
    """
    grouped: dict[str, list[dict[str, object]]] = {}
    for task in tasks:
        intersection_id = str(task["intersection_id"])
        grouped.setdefault(intersection_id, []).append(task)
    ranked = sorted(
        grouped,
        key=lambda intersection_id: (
            -max(as_float(task["cdi"]) for task in grouped[intersection_id]),
            -len(grouped[intersection_id]),
            intersection_id,
        ),
    )
    chosen = ranked[:intersection_limit]
    if len(chosen) > task_limit:
        chosen = chosen[:task_limit]
    selected: list[dict[str, object]] = []
    used: set[str] = set()
    for intersection_id in chosen:
        best = max(
            grouped[intersection_id],
            key=lambda task: (
                as_float(task["cdi"]),
                -as_int(task["ei_min"]),
                str(task["id"]),
            ),
        )
        selected.append(best)
        used.add(str(best["id"]))
    remainder = [
        task
        for intersection_id in chosen
        for task in grouped[intersection_id]
        if str(task["id"]) not in used
    ]
    remainder.sort(
        key=lambda task: (
            -as_float(task["cdi"]),
            as_int(task["ei_min"]),
            str(task["id"]),
        )
    )
    for task in remainder:
        if len(selected) >= task_limit:
            break
        selected.append(task)
    selected.sort(
        key=lambda task: (str(task["intersection_id"]), as_int(task["ei_min"]))
    )
    return chosen, selected


def _allocate(capacities: Sequence[int], count: int) -> list[int]:
    total = sum(capacities)
    if count > total:
        raise ValueError("抽样数量超过总体")
    if total == 0 or count == 0:
        return [0 for _ in capacities]
    raw = [capacity / total * count for capacity in capacities]
    allocation = [
        min(math.floor(value), capacities[index])
        for index, value in enumerate(raw)
    ]
    remain = count - sum(allocation)
    order = sorted(
        range(len(capacities)),
        key=lambda index: (
            raw[index] - math.floor(raw[index]),
            capacities[index],
        ),
        reverse=True,
    )
    guard = 0
    while remain > 0 and guard < 10000:
        progressed = False
        for index in order:
            if remain == 0:
                break
            if allocation[index] < capacities[index]:
                allocation[index] += 1
                remain -= 1
                progressed = True
        if not progressed:
            break
        guard += 1
    if remain:
        raise RuntimeError("无法按比例分配抽样名额")
    return allocation


def _rng(name: str) -> random.Random:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def stratified_sample(
    items: Sequence[dict[str, object]], count: int, name: str
) -> list[dict[str, object]]:
    """按点位所在象限的数量比例抽样,保证小算例不挤在一个角落。"""
    if count > len(items):
        raise ValueError(f"{name} 需要 {count} 个点,总体只有 {len(items)} 个")
    if count == len(items):
        return list(items)
    lons = sorted(as_float(item["lon"]) for item in items)
    lats = sorted(as_float(item["lat"]) for item in items)
    median_lon = lons[len(lons) // 2]
    median_lat = lats[len(lats) // 2]
    groups: list[list[dict[str, object]]] = [[], [], [], []]
    for item in items:
        quadrant = int(as_float(item["lon"]) >= median_lon) + 2 * int(
            as_float(item["lat"]) >= median_lat
        )
        groups[quadrant].append(item)
    allocation = _allocate([len(group) for group in groups], count)
    generator = _rng(name)
    chosen: list[dict[str, object]] = []
    for group, quota in zip(groups, allocation):
        chosen.extend(generator.sample(list(group), quota))
    chosen.sort(key=lambda item: str(item["id"]))
    return chosen


def instance_name(
    monitoring_points: int, landing_pads: int, replicate: int
) -> str:
    return f"M{monitoring_points}L{landing_pads}-{replicate}"


def make_small_instances(
    points: Sequence[dict[str, object]],
    tasks: Sequence[dict[str, object]],
    pads: Sequence[dict[str, object]],
    series: Sequence[tuple[int, int]] = SMALL_SERIES,
    replicates: int = REPLICATES,
) -> tuple[list[dict[str, object]], list[str]]:
    """从大规模集合按比例抽小算例。点数不够的规格记入 skipped。"""
    tasks_by_point: dict[str, list[dict[str, object]]] = {}
    for task in tasks:
        tasks_by_point.setdefault(str(task["intersection_id"]), []).append(
            task
        )
    instances: list[dict[str, object]] = []
    skipped: list[str] = []
    for n_points, n_pads in series:
        for replicate in range(1, replicates + 1):
            name = instance_name(n_points, n_pads, replicate)
            if n_points > len(points) or n_pads > len(pads):
                skipped.append(name)
                continue
            sampled_points = stratified_sample(
                points, n_points, name + ":points"
            )
            sampled_pads = stratified_sample(pads, n_pads, name + ":pads")
            sampled_tasks: list[dict[str, object]] = []
            for point in sampled_points:
                sampled_tasks.extend(tasks_by_point.get(str(point["id"]), []))
            sampled_tasks.sort(
                key=lambda task: (
                    str(task["intersection_id"]),
                    as_int(task["ei_min"]),
                )
            )
            instances.append(
                {
                    "name": name,
                    "monitoring_points": sampled_points,
                    "tasks": sampled_tasks,
                    "landing_pads": sampled_pads,
                }
            )
    return instances, skipped
