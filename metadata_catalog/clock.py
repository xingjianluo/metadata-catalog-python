"""单调逻辑时钟，为每次提交生成单调递增的快照号。

快照号 0 表示空初始状态；每次成功提交（登记/演进/建边/撤边/退役/授权）
通过 ``tick()`` 取到一个新的、严格递增的快照号。
"""
from __future__ import annotations

import threading


class LogicalClock:
    def __init__(self, start: int = 0) -> None:
        self._lock = threading.Lock()
        self._value = start

    def current(self) -> int:
        with self._lock:
            return self._value

    def tick(self) -> int:
        with self._lock:
            self._value += 1
            return self._value
