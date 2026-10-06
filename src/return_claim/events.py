"""事件存储：只追加日志，内存哈希 + JSONL 持久化。

每个事件有 case 内单调连续的 ``seq``。命令在 ``aggregate_update`` 上下文中
完成「加载投影 → 校验 → 追加事件 → 投影回放」，由每 case 一把可重入锁
保证原子性：要么事件与投影一起成功，要么抛错且不留任何痕迹。
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from .models import Event, EventType


class EventStore:
    """线程安全的只追加事件存储。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._file_lock = threading.Lock()
        if self._path and self._path.exists():
            self._load()

    # ---- 持久化 ----------------------------------------------------------

    def _load(self) -> None:
        assert self._path is not None
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            data = json.loads(raw)
            event = Event(
                seq=data["seq"],
                case_id=data["case_id"],
                type=EventType(data["type"]),
                actor=data["actor"],
                reason=data["reason"],
                payload=data["payload"],
            )
            self._events[event.case_id].append(event)

    def _persist(self, event: Event) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False,
                                    sort_keys=True) + "\n")
                fh.flush()

    # ---- 访问 ------------------------------------------------------------

    def events_for(self, case_id: str) -> list[Event]:
        """返回 case 的事件副本（按 seq 升序）。"""
        with self._locks[case_id]:
            return list(self._events.get(case_id, ()))

    def all_case_ids(self) -> list[str]:
        return sorted(self._events.keys())

    def lock_for(self, case_id: str) -> threading.RLock:
        return self._locks[case_id]

    def next_seq(self, case_id: str) -> int:
        """在 case 锁内预读下一个事件序号。"""
        with self._locks[case_id]:
            return len(self._events[case_id]) + 1

    def stage(self, case_id: str, type_: EventType, actor: str, reason: str,
              payload: dict[str, Any]) -> Event:
        """构造事件但不落盘；调用方校验投影无误后再 ``commit``。"""
        return Event(
            seq=self.next_seq(case_id),
            case_id=case_id,
            type=type_,
            actor=actor,
            reason=reason,
            payload=dict(payload),
        )

    def commit(self, event: Event) -> None:
        """提交已暂存事件；必须在对应 case 锁内调用。"""
        history = self._events[event.case_id]
        if event.seq != len(history) + 1:
            raise AssertionError(
                f"事件序号冲突：期望 {len(history) + 1}，得到 {event.seq}"
            )
        self._persist(event)
        history.append(event)

    def append(self, case_id: str, type_: EventType, actor: str, reason: str,
               payload: dict[str, Any]) -> Event:
        """构造并提交一个事件（无投影前校验需求时使用）。"""
        event = self.stage(case_id, type_, actor, reason, payload)
        self.commit(event)
        return event
