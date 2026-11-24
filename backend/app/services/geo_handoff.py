"""市级交接 / 版本核对领域服务。

版本一致性逻辑归属分析快照限界上下文（app.services.geo_snapshot），
本模块仅提供按 ``geo_handoff`` 名称导入的服务入口，方便资源路由与验收代码定位。
"""
from __future__ import annotations

from app.services.geo_snapshot import GeoHandoffService, GeoSnapshotService, HandoffDoc, WindowList

__all__ = ["GeoHandoffService", "GeoSnapshotService", "HandoffDoc", "WindowList"]
