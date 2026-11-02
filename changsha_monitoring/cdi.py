"""拥堵延时指数、监测时长和时间窗。"""

from __future__ import annotations

from dataclasses import dataclass

# 高德矩形接口给出的是车速,不直接给 CDI。
# CDI = 自由流速度 / 实测速度,等于实际行程时间 / 自由流行程时间。
TASK_CDI = 1.4
FREEFLOW_FLOOR_KMH = 50.0
SLOT_MINUTES = 15
# 不到 2 小时的样本里,最高速度往往就是夜间畅行速度,不能再抬到 50。
MIN_SAMPLES_FOR_FLOOR = 8
# 还没有自由流样本时,用高德路况状态兜底。1 是畅通。
STATUS_FALLBACK_CDI = {"2": 1.6, "3": 2.0, "4": 2.8}


def free_flow_speed(speeds_kmh: list[float]) -> float:
    """整天都偏慢时,用 50 km/h 作为主要道路的自由流下限。"""
    if not speeds_kmh:
        return FREEFLOW_FLOOR_KMH
    return max(max(speeds_kmh), FREEFLOW_FLOOR_KMH)


def congestion_delay_index(
    free_flow_kmh: float, speed_kmh: float
) -> float | None:
    if free_flow_kmh <= 0 or speed_kmh <= 0:
        return None
    return free_flow_kmh / speed_kmh


def series_baseline(
    speeds_kmh: list[float], statuses: list[str]
) -> tuple[float, bool]:
    """返回自由流速度,以及能不能用速度比作为 CDI。

    见过畅通、最高速度已经达到 50 km/h,或样本够 2 小时时,用速度比。
    只有一两拍并且高德标成畅通时,不用 50 km/h 把正常低速算成拥堵。
    """
    if not speeds_kmh:
        return FREEFLOW_FLOOR_KMH, False
    peak = max(speeds_kmh)
    saw_smooth = "1" in statuses
    enough = len(speeds_kmh) >= MIN_SAMPLES_FOR_FLOOR
    if saw_smooth or peak >= FREEFLOW_FLOOR_KMH or enough:
        if enough and not saw_smooth and peak < FREEFLOW_FLOOR_KMH:
            return max(peak, FREEFLOW_FLOOR_KMH), True
        return peak, True
    return peak, False


def cdi_for_observation(
    free_flow_kmh: float,
    use_ratio: bool,
    speed_kmh: float,
    status: str,
) -> float | None:
    """有自由流基线时用速度比,否则只把缓行及以上当成拥堵。"""
    if use_ratio:
        return congestion_delay_index(free_flow_kmh, speed_kmh)
    fallback = STATUS_FALLBACK_CDI.get(status)
    if fallback is not None:
        return fallback
    if speed_kmh <= 0:
        return None
    return 1.0


def service_minutes(cdi: float) -> int:
    """按拥堵分档给出监测时长。

    CDI 小于 1.8 为 3 分钟,1.8 到 2.5(含端点)为 6 分钟,
    大于 2.5 为 9 分钟。
    """
    if cdi < 1.8:
        return 3
    if cdi <= 2.5:
        return 6
    return 9


def minute_label(minute: int) -> str:
    if minute == 24 * 60:
        return "24:00"
    bounded = minute % (24 * 60)
    return f"{bounded // 60:02d}:{bounded % 60:02d}"


@dataclass(frozen=True)
class TimeWindowTask:
    ei_min: int
    li_min: int
    cdi: float
    service_min: int

    def as_dict(self, task_id: str, intersection_id: str) -> dict[str, object]:
        return {
            "id": task_id,
            "intersection_id": intersection_id,
            "ei_min": self.ei_min,
            "li_min": self.li_min,
            "ei": minute_label(self.ei_min),
            "li": minute_label(self.li_min),
            "service_min": self.service_min,
            "cdi": round(self.cdi, 4),
        }


def tasks_from_samples(
    samples: list[tuple[int, float]],
) -> list[TimeWindowTask]:
    """把 CDI 大于 1.4 的连续 15 分钟槽合并成一个监测任务。

    时间从当地零点起算,单位分钟。跨过零点的拥堵拆成两段,
    因为 23:45 与 00:00 在分钟数上不相邻。
    监测时长按该时间窗内的最大 CDI 分档。
    """
    congested = sorted(
        (minute, cdi) for minute, cdi in samples if cdi > TASK_CDI
    )
    if not congested:
        return []
    groups: list[list[tuple[int, float]]] = [[congested[0]]]
    for minute, cdi in congested[1:]:
        if minute - groups[-1][-1][0] == SLOT_MINUTES:
            groups[-1].append((minute, cdi))
        else:
            groups.append([(minute, cdi)])
    tasks: list[TimeWindowTask] = []
    for group in groups:
        peak = max(cdi for _, cdi in group)
        ei_min = group[0][0]
        li_min = min(group[-1][0] + SLOT_MINUTES, 24 * 60)
        tasks.append(
            TimeWindowTask(
                ei_min=ei_min,
                li_min=li_min,
                cdi=peak,
                service_min=service_minutes(peak),
            )
        )
    return tasks
