"""日榜结算持久化仓库：日榜输入、领取记录、结算证据、冻结快照与复算记录。

真实项目里这里会换成数据库访问层；当前实现用 JSON 文件落盘，
保证服务重启后已定稿的榜单快照与结算证据仍可核对、排序来路不丢失。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

TABLES = ("boards", "claims", "evidence", "snapshots", "recomputes")


class SettlementStore:
    """文件落盘的日榜结算仓库，内置可重入锁支撑并发定稿只写一次。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {name: {} for name in TABLES}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for name in TABLES:
            value = raw.get(name)
            if isinstance(value, dict):
                self._data[name] = value

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # 读写方法内部都会拿 self.lock（RLock 可重入），
    # 复合的"检查后写入"由 *_once 方法在同一把锁里完成。

    def get_board(self, board_date: str) -> dict[str, Any] | None:
        with self.lock:
            return self._data["boards"].get(board_date)

    def put_board(self, board: dict[str, Any]) -> None:
        with self.lock:
            self._data["boards"][board["board_date"]] = board
            self._save()

    def list_boards(self) -> list[dict[str, Any]]:
        with self.lock:
            return sorted(
                self._data["boards"].values(), key=lambda row: row["board_date"]
            )

    def append_claim(self, claim: dict[str, Any]) -> None:
        with self.lock:
            self._data["claims"].setdefault(claim["board_date"], []).append(claim)
            self._save()

    def latest_claim(self, board_date: str) -> dict[str, Any] | None:
        with self.lock:
            claims = self._data["claims"].get(board_date) or []
            return claims[-1] if claims else None

    def list_claims(self, board_date: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self._data["claims"].get(board_date) or [])

    def get_evidence(self, board_date: str) -> dict[str, Any] | None:
        with self.lock:
            return self._data["evidence"].get(board_date)

    def save_evidence_once(self, evidence: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """并发确认只写一次：已存在定稿证据时直接返回既有证据。"""
        with self.lock:
            existing = self._data["evidence"].get(evidence["board_date"])
            if existing is not None:
                return existing, False
            self._data["evidence"][evidence["board_date"]] = evidence
            self._save()
            return evidence, True

    def get_snapshot(self, board_date: str) -> dict[str, Any] | None:
        with self.lock:
            return self._data["snapshots"].get(board_date)

    def save_snapshot_once(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """冻结快照同样只写一次：重新进入已确认日榜时返回既有快照。"""
        with self.lock:
            existing = self._data["snapshots"].get(snapshot["board_date"])
            if existing is not None:
                return existing, False
            self._data["snapshots"][snapshot["board_date"]] = snapshot
            self._save()
            return snapshot, True

    def append_recompute(self, record: dict[str, Any]) -> None:
        with self.lock:
            self._data["recomputes"].setdefault(record["board_date"], []).append(record)
            self._save()

    def list_recomputes(self, board_date: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self._data["recomputes"].get(board_date) or [])
