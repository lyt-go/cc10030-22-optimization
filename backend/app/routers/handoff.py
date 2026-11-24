"""分析快照版本一致性 / 交接 / 核对 / 区域更正的 HTTP 接口。

路由保持薄层：所有业务判断都在 GeoSnapshotService 内。
另外提供 ``invoke/{method_name}`` 通用分派，便于以 JSON 参数调用任意领域方法。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.services import service_for

router = APIRouter(prefix="/api", tags=["分析快照版本一致性与交接"])


def _svc():
    svc = service_for("geo_snapshot")
    if svc is None:  # pragma: no cover - 注册表兜底
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
    return svc


def _call(method_name: str, payload: dict[str, Any]) -> Any:
    svc = _svc()
    fn = getattr(svc, method_name, None)
    if method_name.startswith("_") or not callable(fn):
        raise HTTPException(status_code=404, detail=f"geo_snapshot 不存在可调用方法：{method_name}")
    try:
        return fn(**(payload or {}))
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"参数不匹配：{exc}") from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 通用 JSON 方法分派（自动化核对 / 脚本可直接按方法名调用） ----------

@router.post("/geo_snapshot/invoke/{method_name}")
def invoke_snapshot_method(method_name: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _call(method_name, payload)
    return {"ok": True, "method": method_name, "result": result}


@router.get("/geo_snapshot/methods")
def list_snapshot_methods() -> dict[str, Any]:
    svc = _svc()
    names = [n for n in dir(svc) if not n.startswith("_") and callable(getattr(svc, n))]
    return {"methods": names}


# ---------- 版本台账 / 热力版本 / 核对 ----------

@router.get("/geo_snapshot/ledger/{scope}")
def get_ledger(scope: str) -> dict[str, Any]:
    return _svc().ledger_state(scope)


@router.get("/geo_snapshot/by-batch/{batch_key}")
def get_snapshot_by_batch(batch_key: str) -> dict[str, Any]:
    return _svc().get_snapshot(batch_key)


@router.post("/geo_snapshot/versions")
def record_version(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("record_heat_version", payload)


@router.get("/geo_snapshot/versions/{scope}")
def list_versions(scope: str, window: str | None = None) -> Any:
    return _svc().list_heat_versions(scope=scope, window=window)


@router.post("/geo_snapshot/reconcile")
def reconcile(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("reconcile_heat_window", payload)


@router.post("/geo_snapshot/window-view")
def window_view(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("window_view", payload)


@router.get("/geo_snapshot/windows/{scope}")
def windows(scope: str) -> Any:
    svc = _svc()
    ledger = svc.ledger_state(scope)
    return [svc.window_view(scope, cw) for cw in ledger.get("order", [])]


# ---------- 区域收数 / 更正 ----------

@router.post("/geo_snapshot/region-confirm")
def region_confirm(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("confirm_region_numbers", payload)


@router.post("/geo_snapshot/corrections")
def start_correction(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("start_region_correction", payload)


@router.get("/geo_snapshot/corrections/{correction_id}")
def get_correction(correction_id: str) -> dict[str, Any]:
    doc = _svc().get_correction(correction_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"更正单不存在：{correction_id}")
    return doc


@router.post("/geo_snapshot/corrections/{correction_id}/apply")
def apply_correction(correction_id: str) -> dict[str, Any]:
    return _svc().apply_region_correction(correction_id)


# ---------- 分项选择确认 ----------

@router.post("/geo_snapshot/confirm")
def confirm_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("confirm_snapshot", payload)


# ---------- 交接单 ----------

@router.post("/geo_snapshot/handoffs")
def issue_handoff(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("issue_handoff", payload)


@router.get("/geo_snapshot/handoffs")
def list_handoffs(scope: str | None = None) -> dict[str, Any]:
    return {"items": _svc().list_handoffs(scope), "total": len(_svc().list_handoffs(scope))}


@router.get("/geo_snapshot/handoffs/current")
def current_handoff(scope: str | None = None) -> Any:
    doc = _svc().get_current_handoff(scope)
    if not doc:
        raise HTTPException(status_code=404, detail="当前还没有已发出的交接单")
    return doc


@router.get("/geo_snapshot/handoffs/{handoff_id}")
def get_handoff(handoff_id: str) -> Any:
    doc = _svc().get_handoff(handoff_id)
    if not doc:
        raise HTTPException(status_code=404, detail=f"交接单不存在：{handoff_id}")
    return doc


@router.get("/geo_snapshot/handoffs/{handoff_id}/workspace")
def handoff_workspace(handoff_id: str) -> Any:
    return _svc().get_handoff_workspace(handoff_id)


# ---------- 一键构造验收场景 ----------

@router.post("/geo_snapshot/scenario/city-handoff")
def city_handoff_scenario(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("build_city_handoff_scenario", payload)


# ---------- 顶层 /handoff 同义入口，便于按资源名发现 ----------

@router.post("/handoff/issue")
def handoff_issue(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("issue_handoff", payload)


@router.get("/handoff/current")
def handoff_current(scope: str | None = None) -> Any:
    doc = _svc().get_current_handoff(scope)
    if not doc:
        raise HTTPException(status_code=404, detail="当前还没有已发出的交接单")
    return doc


@router.post("/handoff/start-correction")
def handoff_start_correction(payload: dict[str, Any] = Body(default_factory=dict)) -> Any:
    return _call("start_region_correction", payload)


@router.post("/handoff/invoke/{method_name}")
def handoff_invoke(method_name: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return invoke_snapshot_method(method_name, payload)
