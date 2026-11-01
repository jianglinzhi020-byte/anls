"""城郊路况采集与算例生成的离线测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from changsha_monitoring.__main__ import main
from changsha_monitoring.amap import AmapClient, AmapError
from changsha_monitoring.cdi import (
    free_flow_speed,
    service_minutes,
    tasks_from_samples,
)
from changsha_monitoring.collect import SHANGHAI, collect_once, collect_series
from changsha_monitoring.geo import haversine_m
from changsha_monitoring.instances import (
    instance_name,
    make_small_instances,
    select_large,
    stratified_sample,
)
from changsha_monitoring.landing_pads import curated_landing_pads
from changsha_monitoring.pipeline import build_dataset
from changsha_monitoring.region import traffic_tiles

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_tiles_stay_inside_gaode_limit() -> None:
    tiles = traffic_tiles()
    assert len(tiles) > 0
    for tile in tiles:
        assert tile.diagonal_m() < 10_000
        assert ";" in tile.rectangle()


def test_landing_pads_are_thirty_periurban_sites() -> None:
    pads = curated_landing_pads()
    assert len(pads) == 30
    assert len({pad.pad_id for pad in pads}) == 30
    assert "梅溪湖" in {pad.name for pad in pads}
    for pad in pads:
        assert 5_000 <= pad.distance_m() <= 16_500
    for left in range(len(pads)):
        for right in range(left + 1, len(pads)):
            distance = haversine_m(
                pads[left].lon,
                pads[left].lat,
                pads[right].lon,
                pads[right].lat,
            )
            assert distance > 1_000


def test_service_duration_brackets() -> None:
    assert service_minutes(1.5) == 3
    assert service_minutes(1.799) == 3
    assert service_minutes(1.8) == 6
    assert service_minutes(2.5) == 6
    assert service_minutes(2.5001) == 9


def test_tasks_merge_slots_and_skip_threshold() -> None:
    tasks = tasks_from_samples(
        [
            (480, 1.4),
            (495, 1.5),
            (510, 2.4),
            (600, 3.0),
            (1425, 2.0),
            (0, 2.0),
            (15, 1.6),
        ]
    )
    assert len(tasks) == 4
    assert tasks[0].ei_min == 0
    assert tasks[0].li_min == 30
    assert tasks[0].service_min == 6
    assert tasks[1].ei_min == 495
    assert tasks[1].li_min == 525
    assert tasks[1].service_min == 6
    assert tasks[2].ei_min == 600
    assert tasks[2].service_min == 9
    assert tasks[3].ei_min == 1425
    assert tasks[3].li_min == 1440
    assert tasks[3].service_min == 6
    assert free_flow_speed([20.0]) == 50.0
    assert free_flow_speed([20.0, 80.0]) == 80.0


def test_large_instance_caps_at_paper_scale() -> None:
    tasks = []
    for index in range(100):
        intersection_id = f"I{index:03d}"
        for slot, cdi in enumerate((1.5, 2.2, 3.0)):
            tasks.append(
                {
                    "id": f"{intersection_id}-{slot}",
                    "intersection_id": intersection_id,
                    "ei_min": 480 + slot * 60,
                    "cdi": cdi + index / 10_000,
                }
            )
    chosen, selected = select_large(tasks)
    assert len(chosen) == 89
    assert len(selected) == 135
    covered = {str(task["intersection_id"]) for task in selected}
    assert covered == set(chosen)


def test_small_instance_names_and_stratified_sample() -> None:
    assert instance_name(10, 5, 2) == "M10L5-2"
    points = [
        {"id": f"I{index:03d}", "lon": 112.9 + index * 0.01, "lat": 28.1}
        for index in range(12)
    ]
    first = stratified_sample(points, 10, "M10L5-1:points")
    second = stratified_sample(points, 10, "M10L5-1:points")
    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert len(first) == 10
    pads = [
        {"id": f"LP{index:02d}", "lon": 113.0, "lat": 28.2 + index * 0.01}
        for index in range(30)
    ]
    tasks = [
        {
            "id": f"T{index:04d}",
            "intersection_id": f"I{index:03d}",
            "ei_min": 480,
            "cdi": 2.0,
        }
        for index in range(12)
    ]
    instances, skipped = make_small_instances(points, tasks, pads)
    assert skipped == []
    names = [str(instance["name"]) for instance in instances]
    assert "M10L5-2" in names
    assert len(instances) == 20
    sample = next(item for item in instances if item["name"] == "M10L5-2")
    assert isinstance(sample["monitoring_points"], list)
    assert isinstance(sample["landing_pads"], list)
    assert isinstance(sample["tasks"], list)
    assert len(sample["monitoring_points"]) == 10
    assert len(sample["landing_pads"]) == 5
    assert len(sample["tasks"]) == 10


def _road(
    name: str, direction: str, speed: float, polyline: str
) -> dict[str, object]:
    return {
        "name": name,
        "direction": direction,
        "status": "3",
        "angle": "0",
        "speed_kmh": speed,
        "lcodes": "",
        "polyline": polyline,
        "tile_id": "test",
    }


def _snapshot(
    stamp: str, speed_main: float, speed_cross: float
) -> dict[str, object]:
    vertical = "112.900000,28.198000;112.900000,28.202000"
    horizontal = "112.896000,28.200000;112.904000,28.200000"
    return {
        "schema": 1,
        "collected_at": stamp,
        "roads": [
            _road("枫林路", "由南向北", speed_main, vertical),
            _road("麓谷大道", "由东向西", speed_cross, horizontal),
        ],
    }


def test_build_dataset_from_crossing_roads(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    payloads = [
        _snapshot("2026-11-02T02:00:00+08:00", 60, 60),
        _snapshot("2026-11-02T08:00:00+08:00", 30, 30),
        _snapshot("2026-11-02T08:15:00+08:00", 25, 25),
    ]
    for index, payload in enumerate(payloads):
        (snapshot_dir / f"snap{index}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    summary = build_dataset(snapshot_dir, tmp_path / "out")
    assert summary["intersections_with_tasks"] == 1
    large = summary["large"]
    assert isinstance(large, dict)
    assert large["intersections"] == 1
    assert large["tasks"] == 1
    assert large["landing_pads"] == 30
    assert large["target_met"] is False
    tasks = json.loads(
        (tmp_path / "out" / "tasks.json").read_text(encoding="utf-8")
    )
    assert tasks[0]["ei"] == "08:00"
    assert tasks[0]["li"] == "08:30"
    assert tasks[0]["service_min"] == 6
    assert tasks[0]["cdi"] == 2.4
    assert (tmp_path / "out" / "roads.jsonl").exists()
    assert summary["skipped_instances"]
    assert summary["instances"] == []


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 8, 0, tzinfo=SHANGHAI)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _ok_payload() -> bytes:
    body = {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "trafficinfo": {
            "roads": [
                {
                    "name": "枫林路",
                    "status": "2",
                    "direction": "由东向西",
                    "angle": "270",
                    "speed": "36",
                    "lcodes": "1001",
                    "polyline": "112.90,28.20;112.91,28.20",
                }
            ]
        },
    }
    return json.dumps(body).encode("utf-8")


def test_client_hides_key_and_collects_one_slot(tmp_path: Path) -> None:
    calls = {"n": 0}

    def opener(url: str, timeout: float) -> bytes:
        calls["n"] += 1
        assert "secret-key" in url
        assert timeout == 20.0
        return _ok_payload()

    client = AmapClient(
        "secret-key", opener=opener, sleep=lambda _seconds: None
    )
    tile = traffic_tiles()[0]
    payload = client.traffic_rectangle(tile)
    assert payload["status"] == "1"
    path = collect_once(
        client,
        [tile],
        datetime(2026, 11, 2, 8, 7, tzinfo=SHANGHAI),
        tmp_path,
        pause_s=0,
        sleep=lambda _seconds: None,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["roads"][0]["speed_kmh"] == 36
    assert "secret-key" not in path.read_text(encoding="utf-8")

    def fail(url: str, timeout: float) -> bytes:
        assert "secret-key" in url
        return json.dumps(
            {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}
        ).encode("utf-8")

    broken = AmapClient("secret-key", opener=fail, sleep=lambda _seconds: None)
    try:
        broken.traffic_rectangle(tile)
    except AmapError as exc:
        assert "secret-key" not in str(exc)
        assert "INVALID_USER_KEY" in str(exc)
    else:
        raise AssertionError("应当拒绝无效 Key")
    assert calls["n"] == 2


def test_collect_series_is_resumable(tmp_path: Path) -> None:
    calls = {"n": 0}

    def opener(url: str, timeout: float) -> bytes:
        calls["n"] += 1
        return _ok_payload()

    client = AmapClient(
        "secret-key", opener=opener, sleep=lambda _seconds: None
    )
    clock = _Clock()
    first = collect_series(
        client,
        tmp_path,
        hours=0.5,
        interval_min=15,
        max_tiles=1,
        pause_s=0,
        now=clock,
        sleep=clock.sleep,
    )
    assert len(first) == 2
    used = calls["n"]
    assert used == 2
    clock.now = datetime(2026, 11, 2, 8, 0, tzinfo=SHANGHAI)
    second = collect_series(
        client,
        tmp_path,
        hours=0.5,
        interval_min=15,
        max_tiles=1,
        pause_s=0,
        now=clock,
        sleep=clock.sleep,
    )
    assert len(second) == 2
    assert calls["n"] == used


def test_cli_tiles_and_missing_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["tiles", "--limit", "1"]) == 0
    assert "环带矩形" in capsys.readouterr().out
    monkeypatch.delenv("AMAP_KEY", raising=False)
    monkeypatch.delenv("GAODE_KEY", raising=False)
    assert main(["collect", "--once"]) == 2
    assert "AMAP_KEY" in capsys.readouterr().err
