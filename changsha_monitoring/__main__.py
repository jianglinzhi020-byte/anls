"""命令行:采集路况,或由快照生成算例。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from changsha_monitoring.amap import AmapClient, AmapError
from changsha_monitoring.collect import (
    SHANGHAI,
    align_slot,
    circle_for_slot,
    collect_circle_once,
    collect_circle_series,
    collect_once,
    collect_series,
    default_client,
)
from changsha_monitoring.pipeline import build_dataset
from changsha_monitoring.quota import QUOTA_FILE, DailyQuota
from changsha_monitoring.region import (
    CIRCLE_RADIUS_M,
    COLLECTION_HOURS,
    DAILY_CALL_LIMIT,
    SAMPLE_MINUTES,
    traffic_circles,
    traffic_tiles,
)

DEFAULT_SNAPSHOTS = Path("data/changsha/snapshots")
DEFAULT_OUT = Path("data/changsha")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="changsha_monitoring",
        description="读取长沙近郊路网并生成监测任务与算例。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    tiles = sub.add_parser("tiles", help="列出环带上的高德矩形,不访问网络")
    tiles.add_argument("--limit", type=int, default=0, help="只打印前 N 块")

    circles = sub.add_parser("circles", help="列出每日圆形矩阵,不访问网络")
    circles.add_argument("--limit", type=int, default=0, help="只打印前 N 个")

    collect = sub.add_parser("collect", help="调用高德交通态势接口采样")
    collect.add_argument(
        "--hours",
        type=float,
        default=COLLECTION_HOURS,
        help="连续采样小时数,默认 24",
    )
    collect.add_argument(
        "--interval",
        type=int,
        default=SAMPLE_MINUTES,
        help="采样间隔分钟,默认 15",
    )
    collect.add_argument(
        "--once",
        action="store_true",
        help="只采当前这一个 15 分钟槽",
    )
    collect.add_argument(
        "--shape",
        choices=("circle", "rectangle"),
        default="circle",
        help="默认每拍 1 个圆,全天不超过 100 次",
    )
    collect.add_argument(
        "--max-tiles",
        type=int,
        default=0,
        help="矩形模式下只请求前 N 块",
    )
    collect.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_SNAPSHOTS,
        help="快照目录",
    )

    build = sub.add_parser("build", help="由快照生成路口、任务和算例")
    build.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOTS)
    build.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "tiles":
            return _print_tiles(args.limit)
        if args.command == "circles":
            return _print_circles(args.limit)
        if args.command == "collect":
            return _collect(args)
        if args.command == "build":
            return _build(args.snapshots, args.out)
    except (AmapError, FileNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def _print_tiles(limit: int) -> int:
    tiles = traffic_tiles()
    shown = tiles if limit <= 0 else tiles[:limit]
    print(f"环带矩形 {len(tiles)} 块,道路等级 4(主要道路及以上)")
    for tile in shown:
        print(
            f"{tile.tile_id}  {tile.rectangle()}  "
            f"对角线 {tile.diagonal_m():.0f} 米"
        )
    return 0


def _print_circles(limit: int) -> int:
    circles = traffic_circles()
    shown = circles if limit <= 0 else circles[:limit]
    print(
        f"圆形矩阵 {len(circles)} 个,半径 {CIRCLE_RADIUS_M:.0f} 米,"
        f"每拍 1 次,每天不超过 {DAILY_CALL_LIMIT} 次"
    )
    for circle in shown:
        print(
            f"{circle.circle_id}{circle.name}  {circle.location()}  "
            f"半径 {circle.radius_m:.0f} 米"
        )
    return 0


def _collect(args: argparse.Namespace) -> int:
    client = default_client()
    if args.shape == "circle":
        return _collect_circles(args, client)
    print(
        "矩形模式不按每日 100 次封顶,全天会超过额度。",
        file=sys.stderr,
    )
    tiles = traffic_tiles()
    if args.max_tiles:
        tiles = tiles[: args.max_tiles]
    if args.once:
        slot = align_slot(datetime.now(SHANGHAI), args.interval)
        path = collect_once(client, tiles, slot, args.out)
        print(f"已写入 {path}")
        return 0
    paths = collect_series(
        client,
        args.out,
        hours=args.hours,
        interval_min=args.interval,
        max_tiles=args.max_tiles or None,
    )
    print(f"完成 {len(paths)} 个时间槽,目录 {args.out}")
    return 0


def _collect_circles(args: argparse.Namespace, client: AmapClient) -> int:
    quota = DailyQuota(QUOTA_FILE, DAILY_CALL_LIMIT)
    client.bind_before_request(quota.reserve)
    circles = traffic_circles()
    if args.once:
        slot = align_slot(datetime.now(SHANGHAI), args.interval)
        circle = circle_for_slot(slot, circles, args.interval)
        path = collect_circle_once(client, circle, slot, args.out)
        print(f"已写入 {path} {circle.circle_id}{circle.name}")
        print(f"今日已用 {quota.used()} / {DAILY_CALL_LIMIT} 次")
        return 0
    paths, reason = collect_circle_series(
        client,
        args.out,
        hours=args.hours,
        interval_min=args.interval,
        quota=quota,
        circles=circles,
    )
    if reason:
        print(reason)
    print(f"完成 {len(paths)} 个时间槽,目录 {args.out}")
    print(f"今日已用 {quota.used()} / {DAILY_CALL_LIMIT} 次")
    return 0


def _build(snapshot_dir: Path, out_dir: Path) -> int:
    summary = build_dataset(snapshot_dir, out_dir)
    large = summary["large"]
    assert isinstance(large, dict)
    print(
        "路口 {detected} 个,有任务的 {active} 个;"
        "大规模算例 {intersections} 个路口、{tasks} 条任务、"
        "{pads} 个起降点。".format(
            detected=summary["intersections_detected"],
            active=summary["intersections_with_tasks"],
            intersections=large["intersections"],
            tasks=large["tasks"],
            pads=large["landing_pads"],
        )
    )
    if not large["target_met"]:
        print("快照不足 89 个路口和 135 条任务。", file=sys.stderr)
        print("请完成 24 小时采样后再 build。", file=sys.stderr)
    written = summary["instances"]
    if not isinstance(written, list):
        raise AmapError("summary 缺少算例列表")
    print(f"小规模算例 {len(written)} 个,目录 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
