"""日榜结算领域服务：日榜输入版本、归档员领取、定稿证据、冻结快照与备查重算。

业务规则：
- 排名按金额降序，金额并列时按地区编号升序确定并列次序，顺位从 1 连续编号。
- 归档员领取时保存领取瞬间的入选集合（前两名）与输入版本。
- 领取后审核可调整金额，日榜输入版本随之递增，与已领取的旧范围并存为两套候选。
- 定稿确认把实际排名、入选地区、输入版本和两套候选写入日榜结算证据，
  并发确认只写一次；重新进入已确认日榜时读取保存的入选集合。
- 冻结榜单快照只取证据中实际确认的前两名，按允许公开的列输出，
  一次定稿快照的地区、位置及金额都出自同份日榜。
- 备查时可按保存证据重算位置并留下复算记录，重启后历史快照仍可核对。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.settlement_store import SettlementStore

TOP_N = 2  # 日榜入选名额：前两名
PUBLIC_COLUMNS = ("顺位", "地区编号", "地区名称", "金额")  # 榜单快照允许公开的列
SOURCE_CLAIMED = "claimed"  # 沿用已领结果
SOURCE_CURRENT = "current"  # 换用确认的新榜
SOURCES = (SOURCE_CLAIMED, SOURCE_CURRENT)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rank_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按金额降序排名，并列时按地区编号升序定次序，顺位从 1 连续编号。"""
    ordered = sorted(
        entries,
        key=lambda entry: (-float(entry.get("金额", 0) or 0), str(entry.get("地区编号", ""))),
    )
    return [dict(entry, 顺位=index) for index, entry in enumerate(ordered, start=1)]


def project_public(ranked_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把排名行投影成允许公开的列，内部列（如审核备注）不进入榜单快照。"""
    return [
        {column: entry.get(column) for column in PUBLIC_COLUMNS}
        for entry in ranked_entries
    ]


class DailyRankingService:
    ENTITY = "日榜"

    def __init__(self, store: SettlementStore) -> None:
        self.store = store

    # ---- 日榜输入与输入版本 ----

    def register_board(
        self,
        board_date: str,
        entries: list[dict[str, Any]],
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        board_date = str(board_date or "").strip()
        if not board_date:
            return None, "缺少榜单日期"
        if self.store.get_board(board_date) is not None:
            return None, f"日榜 {board_date} 已登记，请用审核调整修改金额"
        cleaned, error = self._clean_entries(entries)
        if error:
            return None, error
        now = _now()
        board = {
            "board_date": board_date,
            "version": 1,
            "entries": cleaned,
            "versions": [{
                "version": 1,
                "entries": [dict(entry) for entry in cleaned],
                "reason": "登记日榜",
                "operator": operator,
                "saved_at": now,
            }],
            "created_at": now,
            "updated_at": now,
        }
        self.store.put_board(board)
        return self._board_view(board), f"日榜 {board_date} 已登记，输入版本第 1 版"

    def adjust_board(
        self,
        board_date: str,
        adjustments: dict[str, Any],
        reason: str = "",
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """审核调整金额：定稿前把调整插在领用与快照生成之间，输入版本随之递增。"""
        board = self.store.get_board(board_date)
        if board is None:
            return None, f"日榜 {board_date} 不存在"
        if self.store.get_evidence(board_date) is not None:
            return None, f"日榜 {board_date} 已定稿，不能再调整金额"
        adjustments = {str(code): value for code, value in (adjustments or {}).items()}
        if not adjustments:
            return None, "缺少调整金额"
        entries = [dict(entry) for entry in board["entries"]]
        known = {str(entry.get("地区编号")) for entry in entries}
        unknown = [code for code in adjustments if code not in known]
        if unknown:
            return None, f"地区编号不在日榜内：{'、'.join(unknown)}"
        for entry in entries:
            code = str(entry.get("地区编号"))
            if code in adjustments:
                amount, error = self._clean_amount(adjustments[code])
                if error:
                    return None, f"地区 {code} 金额不是数字"
                entry["金额"] = amount
        board["entries"] = entries
        board["version"] = int(board.get("version", 1)) + 1
        board["updated_at"] = _now()
        board["versions"].append({
            "version": board["version"],
            "entries": [dict(entry) for entry in entries],
            "reason": reason or "审核调整金额",
            "operator": operator,
            "saved_at": board["updated_at"],
        })
        self.store.put_board(board)
        return self._board_view(board), f"日榜 {board_date} 已调整至第 {board['version']} 版"

    def get_board_view(self, board_date: str) -> dict[str, Any] | None:
        board = self.store.get_board(board_date)
        return self._board_view(board) if board else None

    def list_boards(self) -> list[dict[str, Any]]:
        return [self._board_view(board) for board in self.store.list_boards()]

    # ---- 归档员领取 ----

    def claim_board(
        self,
        board_date: str,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """归档员领取：保存领取瞬间的实际排名、入选集合与输入版本。"""
        board = self.store.get_board(board_date)
        if board is None:
            return None, f"日榜 {board_date} 不存在"
        if self.store.get_evidence(board_date) is not None:
            return None, f"日榜 {board_date} 已定稿，不能再领取"
        ranking = rank_entries(board["entries"])
        claims = self.store.list_claims(board_date)
        claim = {
            "claim_id": f"CLM-{board_date}-{len(claims) + 1:04d}",
            "board_date": board_date,
            "claimed_by": operator or "归档员",
            "input_version": board["version"],
            "ranking": ranking,
            "selected": ranking[:TOP_N],
            "claimed_at": _now(),
        }
        self.store.append_claim(claim)
        return claim, f"日榜 {board_date} 已领取，入选前 {len(claim['selected'])} 名"

    def list_claims(self, board_date: str) -> list[dict[str, Any]]:
        return self.store.list_claims(board_date)

    # ---- 定稿确认：写日榜结算证据 ----

    def confirm_board(
        self,
        board_date: str,
        source: str = SOURCE_CURRENT,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """定稿确认：把实际排名、入选地区、输入版本和两套候选写入结算证据。

        并发确认只写一次：后到的确认拿到首次写入的证据，不会覆盖定稿结果。
        """
        board = self.store.get_board(board_date)
        if board is None:
            return None, f"日榜 {board_date} 不存在"
        if source not in SOURCES:
            return None, f"定稿来源只能是 {'、'.join(SOURCES)}"
        claim = self.store.latest_claim(board_date)
        if source == SOURCE_CLAIMED and claim is None:
            return None, f"日榜 {board_date} 尚无领取记录，无法沿用已领结果"
        current_ranking = rank_entries(board["entries"])
        candidates = {
            SOURCE_CLAIMED: (
                {
                    "input_version": claim["input_version"],
                    "selected": claim["selected"],
                }
                if claim is not None
                else None
            ),
            SOURCE_CURRENT: {
                "input_version": board["version"],
                "selected": current_ranking[:TOP_N],
            },
        }
        if source == SOURCE_CLAIMED:
            finalized_ranking = claim["ranking"]
            finalized_selected = claim["selected"]
            input_version = claim["input_version"]
        else:
            finalized_ranking = current_ranking
            finalized_selected = current_ranking[:TOP_N]
            input_version = board["version"]
        evidence = {
            "evidence_id": f"EVD-{board_date}",
            "board_date": board_date,
            "input_version": input_version,
            "finalized_source": source,
            "actual_ranking": finalized_ranking,
            "selected_regions": finalized_selected,
            "candidates": candidates,
            "confirmed_by": operator or "归档员",
            "confirmed_at": _now(),
        }
        saved, created = self.store.save_evidence_once(evidence)
        if created:
            message = f"日榜 {board_date} 已定稿，结算证据 {saved['evidence_id']} 已写入"
        else:
            message = f"日榜 {board_date} 已定稿，并发确认只写一次，沿用证据 {saved['evidence_id']}"
        return {"evidence": saved, "created": created}, message

    def get_evidence(self, board_date: str) -> dict[str, Any] | None:
        return self.store.get_evidence(board_date)

    # ---- 冻结榜单快照 ----

    def generate_snapshot(
        self,
        board_date: str,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """冻结榜单快照：读取证据中实际确认的前两名，按允许公开的列输出。

        重新进入已确认日榜时读取保存的入选集合，返回与定稿一致的对外行和顺位。
        """
        evidence = self.store.get_evidence(board_date)
        if evidence is None:
            return None, f"日榜 {board_date} 尚未定稿，无法生成冻结快照"
        snapshot = {
            "snapshot_id": f"SNP-{board_date}",
            "board_date": board_date,
            "evidence_id": evidence["evidence_id"],
            "rows": project_public(evidence["selected_regions"]),
            "provenance": {
                "evidence_id": evidence["evidence_id"],
                "input_version": evidence["input_version"],
                "finalized_source": evidence["finalized_source"],
            },
            "created_at": _now(),
        }
        saved, created = self.store.save_snapshot_once(snapshot)
        if created:
            message = f"日榜 {board_date} 冻结快照已生成"
        else:
            message = f"重新进入已确认日榜 {board_date}，返回与定稿一致的榜单快照"
        return {"snapshot": saved, "created": created}, message

    def get_snapshot(self, board_date: str) -> dict[str, Any] | None:
        return self.store.get_snapshot(board_date)

    # ---- 备查重算 ----

    def recompute(
        self,
        board_date: str,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """备查重算：按保存的结算证据重算位置，留下复算记录并核对冻结快照。"""
        evidence = self.store.get_evidence(board_date)
        if evidence is None:
            return None, f"日榜 {board_date} 尚未定稿，无法重算"
        reranked = rank_entries(evidence["actual_ranking"])
        rows = project_public(reranked[:TOP_N])
        snapshot = self.store.get_snapshot(board_date)
        records = self.store.list_recomputes(board_date)
        record = {
            "recompute_id": f"REC-{board_date}-{len(records) + 1:04d}",
            "board_date": board_date,
            "evidence_id": evidence["evidence_id"],
            "rows": rows,
            "matches_evidence": rows == project_public(evidence["selected_regions"]),
            "matches_snapshot": (rows == snapshot["rows"]) if snapshot else None,
            "operator": operator or "备查员",
            "created_at": _now(),
        }
        self.store.append_recompute(record)
        return record, f"日榜 {board_date} 已按保存证据重算位置"

    def list_recomputes(self, board_date: str) -> list[dict[str, Any]]:
        return self.store.list_recomputes(board_date)

    # ---- 内部工具 ----

    def _board_view(self, board: dict[str, Any]) -> dict[str, Any]:
        ranking = rank_entries(board["entries"])
        return {
            "board_date": board["board_date"],
            "version": board["version"],
            "entries": [dict(entry) for entry in board["entries"]],
            "current_ranking": ranking,
            "current_selected": ranking[:TOP_N],
            "versions": list(board["versions"]),
            "finalized": self.store.get_evidence(board["board_date"]) is not None,
            "created_at": board["created_at"],
            "updated_at": board["updated_at"],
        }

    @staticmethod
    def _clean_amount(raw: Any) -> tuple[float | int, str]:
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return 0, "金额不是数字"
        return (int(amount) if amount.is_integer() else amount), ""

    def _clean_entries(
        self,
        entries: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]] | None, str]:
        if not entries:
            return None, "日榜至少需要一个地区行"
        cleaned = []
        seen: set[str] = set()
        for raw in entries:
            code = str((raw or {}).get("地区编号", "")).strip()
            if not code:
                return None, "地区行缺少地区编号"
            if code in seen:
                return None, f"地区编号重复：{code}"
            seen.add(code)
            amount, error = self._clean_amount(raw.get("金额", 0))
            if error:
                return None, f"地区 {code} 金额不是数字"
            entry = dict(raw)
            entry["地区编号"] = code
            entry["地区名称"] = str(raw.get("地区名称") or code)
            entry["金额"] = amount
            cleaned.append(entry)
        return cleaned, ""


def _default_store() -> SettlementStore:
    path = os.environ.get(
        "DAILY_RANKING_STORE_PATH",
        str(Path(__file__).resolve().parents[2] / "data" / "daily_ranking_settlement.json"),
    )
    return SettlementStore(path)


daily_ranking_service = DailyRankingService(_default_store())
