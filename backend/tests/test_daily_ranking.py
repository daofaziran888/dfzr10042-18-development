"""洞察工坊 · 日榜结算证据测试

覆盖日榜结算全链路：
- 排名并列按地区编号定次序，领取结果只有前两名真正入选
- 领取后审核调整金额，已领旧范围与有效新榜并存为两套候选
- 定稿确认只写一次，冻结快照的地区、位置及金额出自同份日榜
- 重新进入已确认日榜返回与定稿一致的对外行和顺位
- 备查按保存证据重算位置，重启后历史快照仍一致
"""
from __future__ import annotations

import threading

import pytest

from app.services.daily_ranking import (
    PUBLIC_COLUMNS,
    DailyRankingService,
    rank_entries,
)
from app.settlement_store import SettlementStore

BOARD_DATE = "2026-11-24"


def make_service(tmp_path):
    return DailyRankingService(SettlementStore(tmp_path / "settlement.json"))


def register_abc(service, board_date=BOARD_DATE):
    """登记任务场景日榜：A 区 20、B 区 18、C 区 18。"""
    board, message = service.register_board(
        board_date,
        [
            {"地区编号": "A", "地区名称": "A区", "金额": 20},
            {"地区编号": "B", "地区名称": "B区", "金额": 18},
            {"地区编号": "C", "地区名称": "C区", "金额": 18},
        ],
        operator="运营",
    )
    assert board is not None, message
    return board


def selected_codes(selected):
    return [row["地区编号"] for row in selected]


# ============================================================
# 排名与并列次序
# ============================================================

def test_rank_tie_break_by_region_code():
    """A 区 20、B 区 18、C 区 18：并列按地区编号定次序，A①B②C③。"""
    ranking = rank_entries([
        {"地区编号": "C", "金额": 18},
        {"地区编号": "A", "金额": 20},
        {"地区编号": "B", "金额": 18},
    ])
    assert [(row["顺位"], row["地区编号"]) for row in ranking] == [
        (1, "A"), (2, "B"), (3, "C"),
    ]


def test_claim_keeps_only_top_two(tmp_path):
    """领取结果应只有 A 和 B 真正进前两名。"""
    service = make_service(tmp_path)
    register_abc(service)
    claim, message = service.claim_board(BOARD_DATE, operator="归档员")
    assert claim is not None, message
    assert selected_codes(claim["selected"]) == ["A", "B"]
    assert [row["顺位"] for row in claim["selected"]] == [1, 2]
    assert claim["input_version"] == 1


# ============================================================
# 领取后调整：两套候选
# ============================================================

def test_adjust_between_claim_and_snapshot_keeps_two_candidates(tmp_path):
    """归档员领取后、正式写快照前，C 经审核改为 22：

    已领取的旧范围（A、B）和有效新榜（C、A）冲突，两套候选都保存进证据。
    """
    service = make_service(tmp_path)
    register_abc(service)
    claim, _ = service.claim_board(BOARD_DATE, operator="归档员")
    assert selected_codes(claim["selected"]) == ["A", "B"]

    board, message = service.adjust_board(
        BOARD_DATE, {"C": 22}, reason="审核调整", operator="审核员")
    assert board is not None, message
    assert board["version"] == 2
    assert selected_codes(board["current_selected"]) == ["C", "A"]

    result, message = service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    assert result is not None, message
    evidence = result["evidence"]
    # 两套候选都真实保存：已领旧范围 A、B；有效新榜 C、A
    assert selected_codes(evidence["candidates"]["claimed"]["selected"]) == ["A", "B"]
    assert evidence["candidates"]["claimed"]["input_version"] == 1
    assert selected_codes(evidence["candidates"]["current"]["selected"]) == ["C", "A"]
    assert evidence["candidates"]["current"]["input_version"] == 2


def test_confirm_claimed_finalizes_old_range(tmp_path):
    """沿用已领结果为 A、B。"""
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")

    result, _ = service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    evidence = result["evidence"]
    assert evidence["finalized_source"] == "claimed"
    assert evidence["input_version"] == 1
    assert selected_codes(evidence["selected_regions"]) == ["A", "B"]
    # 定稿时的实际排名完整保存，并列次序仍按地区编号
    assert [(row["顺位"], row["地区编号"]) for row in evidence["actual_ranking"]] == [
        (1, "A"), (2, "B"), (3, "C"),
    ]


def test_confirm_current_finalizes_new_board(tmp_path):
    """换用确认的新榜则为 C、A。"""
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")

    result, _ = service.confirm_board(BOARD_DATE, source="current", operator="归档员")
    evidence = result["evidence"]
    assert evidence["finalized_source"] == "current"
    assert evidence["input_version"] == 2
    assert selected_codes(evidence["selected_regions"]) == ["C", "A"]
    assert [(row["顺位"], row["地区编号"]) for row in evidence["actual_ranking"]] == [
        (1, "C"), (2, "A"), (3, "B"),
    ]


# ============================================================
# 冻结榜单快照
# ============================================================

def test_snapshot_projects_public_columns_only(tmp_path):
    """冻结快照按允许公开的列输出，内部列（审核备注）不进入快照。"""
    service = make_service(tmp_path)
    board, _ = service.register_board(
        BOARD_DATE,
        [
            {"地区编号": "A", "地区名称": "A区", "金额": 20, "审核备注": "内部口径"},
            {"地区编号": "B", "地区名称": "B区", "金额": 18, "审核备注": "内部口径"},
            {"地区编号": "C", "地区名称": "C区", "金额": 18, "审核备注": "内部口径"},
        ],
    )
    assert board is not None
    service.claim_board(BOARD_DATE, operator="归档员")
    service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")

    result, message = service.generate_snapshot(BOARD_DATE)
    assert result is not None, message
    snapshot = result["snapshot"]
    assert snapshot["rows"] == [
        {"顺位": 1, "地区编号": "A", "地区名称": "A区", "金额": 20},
        {"顺位": 2, "地区编号": "B", "地区名称": "B区", "金额": 18},
    ]
    for row in snapshot["rows"]:
        assert set(row.keys()) == set(PUBLIC_COLUMNS)


def test_snapshot_rows_all_from_same_board_version(tmp_path):
    """一次定稿快照的地区、位置及金额都出自同份日榜，不与新版本混合。"""
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    # 调整金额插在领用与快照生成之间：C→22，A→30
    service.adjust_board(BOARD_DATE, {"C": 22, "A": 30}, reason="审核调整")

    result, _ = service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    assert result["created"] is True
    snapshot_result, _ = service.generate_snapshot(BOARD_DATE)
    rows = snapshot_result["snapshot"]["rows"]
    # 沿用已领：金额仍是第 1 版的 20，不是新版的 30
    assert rows == [
        {"顺位": 1, "地区编号": "A", "地区名称": "A区", "金额": 20},
        {"顺位": 2, "地区编号": "B", "地区名称": "B区", "金额": 18},
    ]
    provenance = snapshot_result["snapshot"]["provenance"]
    assert provenance["input_version"] == 1
    assert provenance["finalized_source"] == "claimed"


def test_snapshot_keeps_ordering_provenance(tmp_path):
    """过去已定稿快照保留实际排序来路。"""
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")
    result, _ = service.confirm_board(BOARD_DATE, source="current", operator="归档员")
    evidence_id = result["evidence"]["evidence_id"]

    snapshot_result, _ = service.generate_snapshot(BOARD_DATE)
    provenance = snapshot_result["snapshot"]["provenance"]
    assert provenance == {
        "evidence_id": evidence_id,
        "input_version": 2,
        "finalized_source": "current",
    }


def test_reenter_confirmed_board_returns_consistent_snapshot(tmp_path):
    """重新进入已确认日榜：读取保存的入选集合，返回与定稿一致的对外行和顺位。"""
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")
    service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")

    first, _ = service.generate_snapshot(BOARD_DATE)
    assert first["created"] is True
    # 重新进入：再次生成返回同一份定稿快照，不按新输入重算
    second, message = service.generate_snapshot(BOARD_DATE)
    assert second["created"] is False
    assert "与定稿一致" in message
    assert second["snapshot"] == first["snapshot"]
    assert selected_codes(second["snapshot"]["rows"]) == ["A", "B"]


# ============================================================
# 并发确认只写一次
# ============================================================

def test_concurrent_confirm_writes_only_once(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")

    results = []

    def worker(source):
        results.append(service.confirm_board(BOARD_DATE, source=source, operator="归档员"))

    threads = [
        threading.Thread(target=worker, args=(source,))
        for source in ["claimed", "current", "claimed", "current",
                       "claimed", "current", "claimed", "current"]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(result is not None for result, _ in results)
    created = [result for result, _ in results if result["created"]]
    assert len(created) == 1  # 并发确认只写一次
    evidence_ids = {result["evidence"]["evidence_id"] for result, _ in results}
    assert len(evidence_ids) == 1
    # 落盘的证据也只有一份，且就是首次写入的那份
    persisted = service.get_evidence(BOARD_DATE)
    assert persisted == created[0]["evidence"]


def test_second_confirm_does_not_overwrite(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")

    first, _ = service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    second, message = service.confirm_board(BOARD_DATE, source="current", operator="归档员")
    assert first["created"] is True
    assert second["created"] is False
    assert "只写一次" in message
    # 后到的确认不能改写已定稿的入选集合
    assert selected_codes(second["evidence"]["selected_regions"]) == ["A", "B"]


# ============================================================
# 备查重算与重启一致性
# ============================================================

def test_recompute_from_saved_evidence(tmp_path):
    """备查时按保存证据重算位置，复算记录可读取。"""
    service = make_service(tmp_path)
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")
    service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    snapshot_result, _ = service.generate_snapshot(BOARD_DATE)

    record, message = service.recompute(BOARD_DATE, operator="备查员")
    assert record is not None, message
    assert record["rows"] == snapshot_result["snapshot"]["rows"]
    assert record["matches_evidence"] is True
    assert record["matches_snapshot"] is True

    records = service.list_recomputes(BOARD_DATE)
    assert len(records) == 1
    assert records[0]["recompute_id"] == record["recompute_id"]


def test_restart_keeps_historical_snapshot_consistent(tmp_path):
    """重启后核对历史快照仍一致：投影行、顺位和复算记录都不变。"""
    store_path = tmp_path / "settlement.json"
    service = DailyRankingService(SettlementStore(store_path))
    register_abc(service)
    service.claim_board(BOARD_DATE, operator="归档员")
    service.adjust_board(BOARD_DATE, {"C": 22}, reason="审核调整")
    service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    snapshot_result, _ = service.generate_snapshot(BOARD_DATE)
    record, _ = service.recompute(BOARD_DATE, operator="备查员")

    # 模拟重启：从落盘文件重新加载
    revived = DailyRankingService(SettlementStore(store_path))
    assert revived.get_snapshot(BOARD_DATE) == snapshot_result["snapshot"]
    assert revived.get_evidence(BOARD_DATE) == service.get_evidence(BOARD_DATE)
    assert revived.list_recomputes(BOARD_DATE) == [record]

    # 重启后重算仍与历史快照一致
    new_record, message = revived.recompute(BOARD_DATE, operator="备查员")
    assert new_record is not None, message
    assert new_record["rows"] == snapshot_result["snapshot"]["rows"]
    assert new_record["matches_snapshot"] is True


# ============================================================
# 边界与错误
# ============================================================

def test_snapshot_requires_confirmed_evidence(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    result, message = service.generate_snapshot(BOARD_DATE)
    assert result is None
    assert "尚未定稿" in message


def test_confirm_claimed_requires_claim(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    result, message = service.confirm_board(BOARD_DATE, source="claimed", operator="归档员")
    assert result is None
    assert "尚无领取记录" in message


def test_adjust_after_confirm_rejected(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    service.confirm_board(BOARD_DATE, source="current", operator="归档员")
    result, message = service.adjust_board(BOARD_DATE, {"C": 22})
    assert result is None
    assert "已定稿" in message


def test_claim_after_confirm_rejected(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    service.confirm_board(BOARD_DATE, source="current", operator="归档员")
    result, message = service.claim_board(BOARD_DATE, operator="归档员")
    assert result is None
    assert "已定稿" in message


def test_register_rejects_duplicate_and_bad_entries(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    board, message = service.register_board(BOARD_DATE, [{"地区编号": "D", "金额": 1}])
    assert board is None
    assert "已登记" in message

    board, message = service.register_board("2026-11-25", [])
    assert board is None
    assert "至少需要一个地区行" in message

    board, message = service.register_board(
        "2026-11-25", [{"地区编号": "A", "金额": 1}, {"地区编号": "A", "金额": 2}])
    assert board is None
    assert "重复" in message


def test_adjust_rejects_unknown_region(tmp_path):
    service = make_service(tmp_path)
    register_abc(service)
    board, message = service.adjust_board(BOARD_DATE, {"Z": 99})
    assert board is None
    assert "不在日榜内" in message
