"""业务路由汇总：日榜结算等专用路由先于通用资源路由注册，避免被动态路径抢先匹配。"""
from __future__ import annotations

from app.routers import daily_ranking, resource

ROUTERS = [daily_ranking, resource]
