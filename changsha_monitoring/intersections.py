"""从主要道路折线里识别交通路口。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from changsha_monitoring.geo import to_lonlat, to_xy
from changsha_monitoring.numbers import as_float
from changsha_monitoring.region import (
    CENTER_LAT,
    CENTER_LON,
    INNER_RADIUS_M,
    OUTER_RADIUS_M,
)

CELL_M = 50.0
SEGMENT_BUCKET_M = 200.0


@dataclass
class SegmentGeom:
    key: str
    name: str
    xys: list[tuple[float, float]] = field(default_factory=list)
    speeds: dict[str, float] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)


def parse_polyline_xy(polyline: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for token in polyline.split(";"):
        token = token.strip()
        if not token:
            continue
        lon_text, lat_text = token.split(",")
        points.append(
            to_xy(float(lon_text), float(lat_text), CENTER_LON, CENTER_LAT)
        )
    return points


def _cross(
    origin: tuple[float, float],
    along: tuple[float, float],
    other: tuple[float, float],
) -> float:
    return (along[0] - origin[0]) * (other[1] - origin[1]) - (
        along[1] - origin[1]
    ) * (other[0] - origin[0])


def segment_cross_point(
    start_a: tuple[float, float],
    end_a: tuple[float, float],
    start_b: tuple[float, float],
    end_b: tuple[float, float],
) -> tuple[float, float] | None:
    """两条线段的严格交点,端点相接不算。"""
    area_a = _cross(start_a, end_a, start_b) * _cross(start_a, end_a, end_b)
    area_b = _cross(start_b, end_b, start_a) * _cross(start_b, end_b, end_a)
    if area_a >= 0 or area_b >= 0:
        return None
    denominator = _cross(start_a, end_a, start_b) - _cross(
        start_a, end_a, end_b
    )
    if abs(denominator) < 1e-9:
        return None
    offset = _cross(start_a, end_a, start_b) / denominator
    return (
        start_b[0] + (end_b[0] - start_b[0]) * offset,
        start_b[1] + (end_b[1] - start_b[1]) * offset,
    )


def _first_cross(
    left: SegmentGeom, right: SegmentGeom
) -> tuple[float, float] | None:
    for index in range(len(left.xys) - 1):
        for other in range(len(right.xys) - 1):
            point = segment_cross_point(
                left.xys[index],
                left.xys[index + 1],
                right.xys[other],
                right.xys[other + 1],
            )
            if point is not None:
                return point
    return None


def _segment_cells(segment: SegmentGeom) -> set[tuple[int, int]]:
    cells: set[tuple[int, int]] = set()
    for east, north in segment.xys:
        cells.add(
            (
                math.floor(east / SEGMENT_BUCKET_M),
                math.floor(north / SEGMENT_BUCKET_M),
            )
        )
    pairs = zip(segment.xys, segment.xys[1:])
    for (east0, north0), (east1, north1) in pairs:
        distance = math.hypot(east1 - east0, north1 - north0)
        steps = int(distance / SEGMENT_BUCKET_M) + 1
        for step in range(steps + 1):
            ratio = step / steps
            east = east0 + (east1 - east0) * ratio
            north = north0 + (north1 - north0) * ratio
            cells.add(
                (
                    math.floor(east / SEGMENT_BUCKET_M),
                    math.floor(north / SEGMENT_BUCKET_M),
                )
            )
    return cells


class _Bucket:
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.keys: set[str] = set()
        self.east: list[float] = []
        self.north: list[float] = []

    def add(self, east: float, north: float, name: str, key: str) -> None:
        self.names.add(name)
        self.keys.add(key)
        self.east.append(east)
        self.north.append(north)


def _crossing_points(
    segments: list[SegmentGeom],
) -> list[tuple[float, float, str, str, str, str]]:
    grid: dict[tuple[int, int], list[int]] = {}
    for index, segment in enumerate(segments):
        for cell in _segment_cells(segment):
            grid.setdefault(cell, []).append(index)
    seen: set[tuple[int, int]] = set()
    hits: list[tuple[float, float, str, str, str, str]] = []
    for indexes in grid.values():
        unique = list(dict.fromkeys(indexes))
        for left_pos in range(len(unique)):
            for right_pos in range(left_pos + 1, len(unique)):
                left_index = unique[left_pos]
                right_index = unique[right_pos]
                pair = (
                    min(left_index, right_index),
                    max(left_index, right_index),
                )
                if pair in seen:
                    continue
                seen.add(pair)
                left = segments[pair[0]]
                right = segments[pair[1]]
                if left.key == right.key or left.name == right.name:
                    continue
                point = _first_cross(left, right)
                if point is None:
                    continue
                hits.append(
                    (
                        point[0],
                        point[1],
                        left.name,
                        right.name,
                        left.key,
                        right.key,
                    )
                )
    return hits


def _merge_seeds(
    seeds: set[tuple[int, int]],
) -> list[set[tuple[int, int]]]:
    remaining = set(seeds)
    groups: list[set[tuple[int, int]]] = []
    while remaining:
        start = remaining.pop()
        stack = [start]
        group = {start}
        while stack:
            row, col = stack.pop()
            for d_row in (-1, 0, 1):
                for d_col in (-1, 0, 1):
                    neighbour = (row + d_row, col + d_col)
                    if neighbour in remaining:
                        remaining.remove(neighbour)
                        group.add(neighbour)
                        stack.append(neighbour)
        groups.append(group)
    return groups


def find_intersections(
    segments: list[SegmentGeom],
) -> list[dict[str, object]]:
    """返回环带内、至少有两条不同道路相交的路口。"""
    named = [
        segment
        for segment in segments
        if segment.name and len(segment.xys) >= 2
    ]
    buckets: dict[tuple[int, int], _Bucket] = {}

    def bucket_for(east: float, north: float) -> _Bucket:
        cell = (round(east / CELL_M), round(north / CELL_M))
        bucket = buckets.get(cell)
        if bucket is None:
            bucket = _Bucket()
            buckets[cell] = bucket
        return bucket

    for segment in named:
        for east, north in segment.xys:
            bucket_for(east, north).add(east, north, segment.name, segment.key)
    for (
        east,
        north,
        left_name,
        right_name,
        left_key,
        right_key,
    ) in _crossing_points(named):
        bucket = bucket_for(east, north)
        bucket.add(east, north, left_name, left_key)
        bucket.add(east, north, right_name, right_key)

    seeds = {
        cell for cell, bucket in buckets.items() if len(bucket.names) >= 2
    }
    intersections: list[dict[str, object]] = []
    for group in _merge_seeds(seeds):
        names: set[str] = set()
        keys: set[str] = set()
        east_values: list[float] = []
        north_values: list[float] = []
        for cell in group:
            bucket = buckets[cell]
            names.update(bucket.names)
            keys.update(bucket.keys)
            east_values.extend(bucket.east)
            north_values.extend(bucket.north)
        if len(names) < 2 or not east_values:
            continue
        east = sum(east_values) / len(east_values)
        north = sum(north_values) / len(north_values)
        distance = math.hypot(east, north)
        if not INNER_RADIUS_M <= distance <= OUTER_RADIUS_M:
            continue
        lon, lat = to_lonlat(east, north, CENTER_LON, CENTER_LAT)
        intersections.append(
            {
                "lon": lon,
                "lat": lat,
                "road_names": sorted(names),
                "segment_keys": sorted(keys),
                "distance_m": distance,
            }
        )
    intersections.sort(
        key=lambda item: (-as_float(item["lat"]), as_float(item["lon"]))
    )
    for index, item in enumerate(intersections, start=1):
        item["id"] = f"I{index:03d}"
    return intersections
