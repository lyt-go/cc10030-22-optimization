"""业务路由汇总：交接/版本一致性专用路由在前，通用资源路由在后。"""
from __future__ import annotations

from app.routers import handoff, resource

ROUTERS = [handoff, resource]
