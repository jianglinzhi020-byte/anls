"""按上海时间的自然日限制高德调用次数。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from changsha_monitoring.amap import QuotaExceeded
from changsha_monitoring.region import DAILY_CALL_LIMIT

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUOTA_FILE = Path("data/changsha/quota.json")


class DailyQuota:
    """发出请求前占一次额度。满额后不再访问网络。"""

    def __init__(
        self,
        path: Path = QUOTA_FILE,
        limit: int = DAILY_CALL_LIMIT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path
        self.limit = limit
        self._now = now or (lambda: datetime.now(SHANGHAI))

    def _today(self) -> str:
        return self._now().astimezone(SHANGHAI).date().isoformat()

    def _load(self) -> dict[str, int | str]:
        today = self._today()
        empty: dict[str, int | str] = {
            "date": today,
            "calls": 0,
            "limit": self.limit,
        }
        if not self.path.is_file():
            return empty
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("date") != today:
            return empty
        calls = loaded.get("calls")
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0:
            calls = 0
        return {"date": today, "calls": calls, "limit": self.limit}

    def _save(self, state: dict[str, int | str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def used(self) -> int:
        calls = self._load()["calls"]
        return calls if isinstance(calls, int) else 0

    def reserve(self) -> int:
        state = self._load()
        calls = state["calls"]
        if not isinstance(calls, int) or calls >= self.limit:
            raise QuotaExceeded(f"今日调用已达 {self.limit} 次,停止请求")
        state["calls"] = calls + 1
        self._save(state)
        return calls + 1

    def exhaust(self) -> None:
        state = self._load()
        state["calls"] = self.limit
        self._save(state)
