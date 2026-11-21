"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import quality_daily, resource

# 固定前缀的日报路由必须先注册，通用资源路由 /api/{module} 才不会抢先匹配。
ROUTERS = [quality_daily, resource]
