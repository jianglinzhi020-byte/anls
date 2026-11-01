"""从 JSON 对象里取出数字。"""

from __future__ import annotations


def as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("需要数字")
    return float(value)


def as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("需要整数")
    return value
