"""版本一致性验收：晚到定位引发热力更正时，快照/交接/区域更正必须对得上。

覆盖题目口径：
- 同一格时窗口保留 8 点旧版与 9 点更正版，区域采用一版、单列差额，绝不报 17；
- 仅写入未核对 / 处理失败的窗口保持「待续」；
- 已确认快照与已发出交接单持久冻结每个分项选择，刷新（新服务实例）后完全一致；
- 热力可发布窗口带实际核对行，旧公开数可按原格时窗口依据回算；
- 市级交接区分独立签发热力与未调账区域，并为只接收已对齐数据的一方保留可核对原数；
- 区域更正必须以交接单发出时的实际 basis 启动，且不改动已发出交接单。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_version_test_"))
    import app.services.base as base_mod
    monkeypatch.setattr(base_mod, "DATA_ROOT", tmpdir)
    for sub in ["geo_points", "coord_transformed", "grid_index", "heatmap",
                "region_aggregates", "snapshots", "handoffs", "region_corrections",
                "snapshot_book/snapshots"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def _service():
    from app.services.geo_snapshot import GeoSnapshotService
    return GeoSnapshotService()


def test_eight_and_nine_versions_never_sum():
    """同一格保有 8 旧版 / 9 更正版：当前热力取 9，区域采用 8，任何总数都不是 17。"""
    svc = _service()
    scope = "SCOPE-8-9"
    svc.record_heat_version(scope, "甲窗", 8, version=1, published=True)
    svc.record_heat_version(scope, "甲窗", 9, version=2)
    svc.reconcile_heat_window(scope, "甲窗", version=2, actual_count=9)
    svc.confirm_region_numbers(scope, "甲窗", 8)

    view = svc.window_view(scope, "甲窗")
    assert view["heatmap"]["point_count"] == 9
    assert view["heatmap"] == 9
    assert view["region"]["point_count"] == 8
    assert view["region"] == 8
    assert view["adopted_heat_uid"] is not None
    assert view["adopted_version"]["point_count"] == 8
    assert view["pending_delta"] == 1
    assert view["待调差数"] == 1
    assert view["status"] == "已核对"
    # 区域收数只报采用值 8
    assert view["region"]["reported_total"] == 8
    # 两版都在
    counts = sorted(v["point_count"] for v in view["heat_versions"])
    assert counts == [8, 9]
    assert 17 not in counts


def test_written_without_check_and_failure_stay_pending():
    """只写入未核对 -> 待续；处理失败 -> 仍待续。"""
    svc = _service()
    svc.record_heat_version("P", "甲窗", 9)  # 未核对
    assert svc.window_view("P", "甲窗")["status"] == "待续"

    svc.mark_window_pending("P", "乙窗", reason="处理失败", failed=True)
    b = svc.window_view("P", "乙窗")
    assert b["status"] == "待续"
    assert b["pending"] is True
    assert "失败" in (b["pending_reason"] or "")

    # 核对通过后才转已核对
    svc.reconcile_heat_window("P", "甲窗", actual_count=9)
    assert svc.window_view("P", "甲窗")["status"] == "已核对"


def test_reconciliation_rows_are_actual_and_old_version_recomputable():
    """核对行包含写入值/回算值/一致性；旧版按自己的 basis 仍可回算出 8。"""
    svc = _service()
    v1 = svc.record_heat_version("R", "甲窗", 8, version=1, basis_count=8)
    v2 = svc.record_heat_version("R", "甲窗", 9, version=2, basis_count=9)

    row2 = svc.reconcile_heat_window("R", "甲窗", uid=v2["uid"])
    assert row2["actual_row"] is True
    assert row2["written_count"] == 9 and row2["recomputed_count"] == 9
    assert row2["matched"] is True

    row1 = svc.reconcile_heat_window("R", "甲窗", uid=v1["uid"])
    assert row1["recomputed_count"] == 8 and row1["matched"] is True

    rows = svc.reconciliation_rows("R")
    assert len(rows) >= 2
    assert all("recomputed_count" in r and "matched" in r for r in rows)


def test_confirmed_snapshot_persists_item_selections_after_refresh():
    """确认快照持久每个分项选择；新建服务实例（刷新）后选择与指纹不变。"""
    svc = _service()
    svc.build_city_handoff_scenario("SNAP-PERSIST", issue=False)
    doc = svc.confirm_snapshot("SNAP-PERSIST")
    assert len(doc["selections"]) >= 3  # 甲 heat+region, 乙只有失败无版本 -> 至少两条以上

    svc2 = _service()
    again = svc2.get_confirmed_snapshot(doc["snapshot_id"])
    assert again["selection_fingerprint"] == doc["selection_fingerprint"]
    keys = {(s["component"], s["window"]) for s in again["selections"]}
    assert ("heatmap", "甲窗") in keys
    assert ("region", "甲窗") in keys
    heat_sel = next(s for s in again["selections"] if s["component"] == "heatmap" and s["window"] == "甲窗")
    assert heat_sel["value"] == 9 and heat_sel["usable"] is True


def test_city_handoff_issue_refresh_and_correction_basis():
    """完整验收脚本：发单 -> 刷新一致 -> 按发出 basis 启动更正 -> 甲 9/8/1，乙待续。"""
    svc = _service()
    result = svc.build_city_handoff_scenario("CITY-X")
    hid = result["handoff_id"]

    # 模拟刷新：全新服务实例从磁盘读取
    refreshed = _service().get_handoff(hid)
    a = refreshed["windows"]["甲窗"]
    b = refreshed["windows"]["乙窗"]

    assert refreshed["level"] == "市级"
    assert a["heatmap"]["point_count"] == 9
    assert a["heatmap"] == 9
    assert a["region"]["point_count"] == 8
    assert a["region"] == 8
    assert a["pending_delta"] == 1
    assert b["status"] == "待续"

    # 交接单指纹刷新后一致（与上次实际交接对得上）
    assert refreshed["receipt"]["fingerprint"] == result["handoff"]["receipt"]["fingerprint"]

    # 区域总数按采用值只计一次 = 8，绝不是 17
    assert refreshed["region_confirmed_total"] == 8

    # 独立签发的热力 9 已在列，区域待调账也单独注明
    issued_heat = refreshed["heatmap_independent_issue"]
    assert any(x["window"] == "甲窗" and x["point_count"] == 9 for x in issued_heat)
    pending_region = refreshed["region_pending_adjustment"]
    assert pending_region[0]["window"] == "甲窗"
    assert pending_region[0]["region_count"] == 8
    assert pending_region[0]["pending_delta"] == 1

    # 只接收已对齐数据的一方拿到可核对原数 8（旧热力=区域采用版）
    originals = refreshed["aligned_package"]["original_figures"]
    assert any(o["window"] == "甲窗" and o["point_count"] == 8 for o in originals)
    aligned_item = refreshed["aligned_package"]["items"][0]
    assert aligned_item["heatmap_version"]["point_count"] == 8
    assert aligned_item["verifiable"] is True

    # 更正单严格基于发出时冻结的 basis
    corr = result["correction"]
    assert corr["based_on_handoff_basis"] is True
    line = corr["lines"][0]
    assert line["window"] == "甲窗"
    assert line["from_points"] == 8 and line["to_points"] == 9
    assert line["adjust_delta"] == 1
    assert corr["basis_fingerprint"] == refreshed["basis"]["basis_fingerprint"]

    # 启动更正后，当前交接单仍冻结为 热力9 / 区域8 / 差1
    current = _service().get_current_handoff("CITY-X")
    ca = current["windows"]["甲窗"]
    assert ca["heatmap"] == 9 and ca["region"] == 8 and ca["pending_delta"] == 1
    assert current["windows"]["乙窗"]["status"] == "待续"


def test_get_snapshot_exposes_persisted_selections():
    """既有 get_snapshot(batch_key) 也能拿到分项选择，兼容旧读取方。"""
    from app.services.geo_source import GeoSourceService
    svc_geo = GeoSourceService()
    svc_geo.onboard_batch({"批次编号": "B-SNAP", "采集终端": "T", "定位方式": "GPS", "采样频率": "1Hz"}, point_count=12)

    svc = _service()
    snap_result = svc.create_snapshot("B-SNAP")
    assert any(s["component"] == "heatmap" for s in snap_result["selections"])
    svc.record_heat_version("B-SNAP", "甲窗", 8)
    svc.confirm_snapshot("B-SNAP")

    snap = svc.get_snapshot("B-SNAP")
    assert "snapshot_id" in snap
    assert snap["confirmed"] is True
    assert isinstance(snap["selections"], list) and snap["selections"]
