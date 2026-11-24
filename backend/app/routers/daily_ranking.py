"""日榜结算接口：日榜登记、审核调整、归档员领取、定稿确认、冻结快照与备查重算。

路由层不做业务判断，状态流转都在 app.services.daily_ranking 里。
注意：本路由必须先于通用资源路由注册，否则 /api/daily_ranking/...
会被 /api/{module}/{entry_id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.daily_ranking import daily_ranking_service

router = APIRouter(prefix="/api/daily_ranking", tags=["日榜结算"])


class BoardPayload(BaseModel):
    board_date: str
    entries: list[dict[str, Any]] = Field(default_factory=list)
    operator: str = ""


class AdjustPayload(BaseModel):
    adjustments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    operator: str = ""


class OperatorPayload(BaseModel):
    operator: str = ""


class ConfirmPayload(BaseModel):
    source: str = "current"
    operator: str = ""


def _unwrap(result: dict[str, Any] | None, message: str) -> dict[str, Any]:
    if result is None:
        status_code = 404 if "不存在" in message else 409
        raise HTTPException(status_code=status_code, detail=message)
    return {"ok": True, "message": message, "data": result}


@router.post("/boards")
def register_board(payload: BoardPayload) -> dict[str, Any]:
    """登记日榜输入，输入版本从第 1 版开始。"""
    board, message = daily_ranking_service.register_board(
        payload.board_date, payload.entries, payload.operator)
    return _unwrap(board, message)


@router.get("/boards")
def list_boards() -> dict[str, Any]:
    return {"items": daily_ranking_service.list_boards()}


@router.get("/boards/{board_date}")
def get_board(board_date: str) -> dict[str, Any]:
    board = daily_ranking_service.get_board_view(board_date)
    if board is None:
        raise HTTPException(status_code=404, detail=f"日榜 {board_date} 不存在")
    return board


@router.post("/boards/{board_date}/adjust")
def adjust_board(board_date: str, payload: AdjustPayload) -> dict[str, Any]:
    """审核调整金额：定稿前把调整插在领用与快照生成之间，输入版本递增。"""
    board, message = daily_ranking_service.adjust_board(
        board_date, payload.adjustments, payload.reason, payload.operator)
    return _unwrap(board, message)


@router.post("/boards/{board_date}/claim")
def claim_board(board_date: str, payload: OperatorPayload) -> dict[str, Any]:
    """归档员领取：保存领取瞬间的入选集合与输入版本。"""
    claim, message = daily_ranking_service.claim_board(board_date, payload.operator)
    return _unwrap(claim, message)


@router.get("/boards/{board_date}/claims")
def list_claims(board_date: str) -> dict[str, Any]:
    return {"items": daily_ranking_service.list_claims(board_date)}


@router.post("/boards/{board_date}/confirm")
def confirm_board(board_date: str, payload: ConfirmPayload) -> dict[str, Any]:
    """定稿确认：写日榜结算证据，并发确认只写一次。"""
    result, message = daily_ranking_service.confirm_board(
        board_date, payload.source, payload.operator)
    return _unwrap(result, message)


@router.get("/boards/{board_date}/evidence")
def get_evidence(board_date: str) -> dict[str, Any]:
    evidence = daily_ranking_service.get_evidence(board_date)
    if evidence is None:
        raise HTTPException(status_code=404, detail=f"日榜 {board_date} 尚未定稿")
    return evidence


@router.post("/boards/{board_date}/snapshot")
def generate_snapshot(board_date: str) -> dict[str, Any]:
    """生成冻结榜单快照：取证据中实际确认的前两名，按允许公开的列输出。"""
    result, message = daily_ranking_service.generate_snapshot(board_date)
    return _unwrap(result, message)


@router.get("/boards/{board_date}/snapshot")
def read_snapshot(board_date: str) -> dict[str, Any]:
    """重新进入已确认日榜：读取保存的入选集合，返回与定稿一致的快照。"""
    snapshot = daily_ranking_service.get_snapshot(board_date)
    if snapshot is None:
        result, message = daily_ranking_service.generate_snapshot(board_date)
        if result is None:
            raise HTTPException(status_code=409, detail=message)
        snapshot = result["snapshot"]
    return snapshot


@router.post("/boards/{board_date}/recompute")
def recompute(board_date: str, payload: OperatorPayload) -> dict[str, Any]:
    """备查重算：按保存证据重算位置，留下复算记录。"""
    record, message = daily_ranking_service.recompute(board_date, payload.operator)
    return _unwrap(record, message)


@router.get("/boards/{board_date}/recomputes")
def list_recomputes(board_date: str) -> dict[str, Any]:
    return {"items": daily_ranking_service.list_recomputes(board_date)}
