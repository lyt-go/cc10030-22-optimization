#!/usr/bin/env python3
"""发出市级交接并按本次发出的实际依据启动区域更正（验收操作脚本）。

运行：/workspace/backend/.venv/bin/python /workspace/scripts/issue_city_handoff.py

脚本会：
1. 用真实网格/热力/区域产物构造甲窗“8 点旧版 -> 晚到 1 点 -> 9 点更正版”，
   区域汇总停留在旧的 8；乙窗构造为重算处理失败；
2. 登记并实际回算核对两版热力，区域显式确认采用 8 点旧版，待调差 +1；
3. 持久发出市级交接单（固定单号，可重复执行重建），冻结每个分项选择；
4. 用交接单发出时的实际 basis 启动区域更正；
5. 用“全新服务实例”模拟刷新，打印甲窗热力/区域/待调差数与乙窗待续状态。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path("/workspace/backend")
sys.path.insert(0, str(BACKEND))

from app.services.base import DATA_ROOT  # noqa: E402
from app.services.geo_snapshot import GeoSnapshotService  # noqa: E402
from app.services.grid_index import GridIndexService  # noqa: E402
from app.services.heatmap_aggregate import HeatmapAggregateService  # noqa: E402
from app.services.region_dim import RegionDimService  # noqa: E402

SCOPE = "CITY-HANDOFF-20261124"
HANDOFF_ID = "HO-CITY-20261124-ISSUED"
CORRECTION_ID = "RCORR-CITY-20261124-BASIS"

LAT0, LON0 = 39.9042, 116.4074
TB = 1789002000  # 整点小时桶


def _point(pid: int, i: int) -> dict:
    return {
        "point_id": pid,
        "batch_key": SCOPE,
        "lat": round(LAT0 + (i % 3) * 0.00012, 6),
        "lon": round(LON0 + (i // 3) * 0.00012, 6),
        "timestamp": TB + 60 + i * 7,
        "terminal": f"TERM-CITY-{pid:02d}",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def main() -> int:
    svc = GeoSnapshotService()
    svc.reset_scope(SCOPE)

    # 清理可能存在的固定交接/更正单，以及该 scope 的派生数据集，保证可重复执行
    for sub, suffix in [
        ("handoffs", f"{HANDOFF_ID}*.json"),
        ("region_corrections", f"{CORRECTION_ID}.json"),
        ("geo_points", f"{SCOPE}.jsonl"),
        ("coord_transformed", f"{SCOPE}_transformed.jsonl"),
        ("grid_index", f"{SCOPE}_grid.jsonl"),
        ("heatmap", f"{SCOPE}_heatmap.jsonl"),
        ("region_aggregates", f"{SCOPE}_region.jsonl"),
        ("snapshots", f"{SCOPE}_snapshot.jsonl"),
    ]:
        d = DATA_ROOT / sub
        if not d.exists():
            continue
        for p in d.glob(suffix):
            p.unlink()

    # ---- 先有 8 个定位点：真实走 网格 -> 热力 -> 区域（区域停在 8） ----
    pts8 = [_point(i + 1, i) for i in range(8)]
    _write_jsonl(DATA_ROOT / "geo_points" / f"{SCOPE}.jsonl", pts8)
    transformed8 = [{
        "point_id": p["point_id"], "batch_key": SCOPE,
        "trans_lat": p["lat"], "trans_lon": p["lon"], "timestamp": p["timestamp"],
    } for p in pts8]
    _write_jsonl(DATA_ROOT / "coord_transformed" / f"{SCOPE}_transformed.jsonl", transformed8)

    GridIndexService().index_points_from_batch(SCOPE)
    HeatmapAggregateService().aggregate_grid_heatmap(SCOPE)
    RegionDimService().aggregate_by_region(SCOPE)

    cells, _ = HeatmapAggregateService().get_heatmap_data(SCOPE)
    assert len(cells) == 1 and cells[0]["point_count"] == 8, cells
    cell = cells[0]
    region_rows, _ = RegionDimService().get_region_aggregates(SCOPE)
    assert region_rows and region_rows[-1]["total_points"] == 8, region_rows

    # ---- 8 点旧版入台账（旧公开数，保留并核对） ----
    v_old = svc.record_heat_version(
        SCOPE, "甲窗", 8, version=1, uid="HV-JIA-v1-8",
        geohash=cell["geohash"], time_bucket=cell["time_bucket"],
        cell_key=f"{cell['geohash']}_{cell['time_bucket']}",
        published=True, old_public=True, source="original_public_release",
        basis_point_ids=list(range(1, 9)), basis_count=8,
    )
    row_old = svc.reconcile_heat_window(SCOPE, "甲窗", uid=v_old["uid"])
    assert row_old["matched"] and row_old["recomputed_count"] == 8

    # ---- 晚到的第 9 点到达，追加后重算网格/热力（覆盖，不追加）；区域故意不重算 ----
    late = _point(9, 1)
    late["is_backfill"] = True
    with open(DATA_ROOT / "geo_points" / f"{SCOPE}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(late, ensure_ascii=False) + "\n")
    with open(DATA_ROOT / "coord_transformed" / f"{SCOPE}_transformed.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "point_id": 9, "batch_key": SCOPE,
            "trans_lat": late["lat"], "trans_lon": late["lon"], "timestamp": late["timestamp"],
        }, ensure_ascii=False) + "\n")

    GridIndexService().index_points_from_batch(SCOPE)
    HeatmapAggregateService().aggregate_grid_heatmap(SCOPE)
    cells2, _ = HeatmapAggregateService().get_heatmap_data(SCOPE)
    assert len(cells2) == 1 and cells2[0]["point_count"] == 9, cells2

    v_new = svc.record_heat_version(
        SCOPE, "甲窗", 9, version=2, uid="HV-JIA-v2-9",
        geohash=cell["geohash"], time_bucket=cell["time_bucket"],
        cell_key=f"{cell['geohash']}_{cell['time_bucket']}",
        source="late_location_backfill_recompute",
        basis_point_ids=list(range(1, 10)), basis_count=9,
    )
    row_new = svc.reconcile_heat_window(SCOPE, "甲窗", uid=v_new["uid"])
    assert row_new["matched"] and row_new["recomputed_count"] == 9

    # ---- 区域收数：仍只确认 8，显式采用 8 点旧版 ----
    svc.confirm_region_numbers(
        SCOPE, "甲窗", 8, uid="RC-JIA-8", adopted_uid="HV-JIA-v1-8",
        region_name=region_rows[-1].get("region_name", "东城区"),
    )

    # ---- 乙窗：处理失败，留待续算 ----
    svc.mark_window_pending(SCOPE, "乙窗", reason="乙窗热力重算任务处理失败（数据源读取异常）", failed=True)

    # ---- 标准分析快照（兼容旧快照读取方），再确认分项选择 ----
    snap_result = svc.create_snapshot(SCOPE)
    svc.confirm_snapshot(SCOPE)

    # ---- 发出市级交接（固定单号） ----
    handoff = svc.issue_handoff(
        SCOPE, handoff_id=HANDOFF_ID, level="市级",
        title="市级数据分析交接：甲窗热力更正独立签发 / 区域待调账；乙窗待续",
    )

    # ---- 用本次发出的实际依据启动区域更正（固定更正单号） ----
    correction = svc.start_region_correction(handoff["handoff_id"], correction_id=CORRECTION_ID)

    # ---- 刷新：全新服务实例从磁盘读取 ----
    refreshed = GeoSnapshotService().get_handoff(HANDOFF_ID)
    a = refreshed["windows"]["甲窗"]
    b = refreshed["windows"]["乙窗"]

    print("=" * 72)
    print("市级交接已发出：", HANDOFF_ID)
    print("数据目录：", DATA_ROOT)
    print("标准快照：", snap_result["snapshot_id"], " total_results =", snap_result["total_results"])
    print("-" * 72)
    print(f"甲窗  热力 = {a['heatmap']['point_count']} 点（{a['heatmap_status']}，版本 {a['heatmap'].get('version')} {a['heatmap']['uid']}）")
    print(f"      区域 = {a['region']['region_count']} 点（采用 {a['region']['adopted_heat_uid']} / {a['region']['adopted_point_count']} 点旧版）")
    print(f"      待调差数 = {a['pending_delta']} （9 - 8 = +1，单独核对，绝不报 17）")
    print(f"      对齐状态 = {a['alignment_status']}")
    print(f"乙窗  状态 = {b['status']}（pending={b['pending']}，原因：{b['pending_reason']}）")
    print("-" * 72)
    print("区域确认总数 =", refreshed["region_confirmed_total"], "（只按采用值计一次 = 8）")
    print("已对齐原数包 =", refreshed["aligned_package"]["original_figures"])
    print("更正单行 =", [(x["window"], f'{x["from_points"]}->{x["to_points"]}', f'delta={x["adjust_delta"]}', x["status"])
                         for x in correction["lines"]])
    print("更正依据指纹 =", correction["basis_fingerprint"])
    print("交接单指纹   =", refreshed["receipt"]["fingerprint"])
    print("=" * 72)

    # 硬性核对
    assert a["heatmap"] == 9 and a["region"] == 8 and a["pending_delta"] == 1
    assert b["status"] == "待续"
    assert refreshed["region_confirmed_total"] == 8
    assert correction["lines"][0]["from_points"] == 8
    assert correction["lines"][0]["to_points"] == 9

    # 落一份紧凑核对视图，方便直接查看
    view = {
        "handoff_id": HANDOFF_ID,
        "correction_id": CORRECTION_ID,
        "scope": SCOPE,
        "甲窗": {
            "热力": a["heatmap_count"], "热力状态": a["heatmap_status"],
            "区域": a["region_count"], "区域采用版本": a["adopted_heat_uid"],
            "待调差数": a["pending_delta"], "对齐状态": a["alignment_status"],
        },
        "乙窗": {"状态": b["status"], "pending": b["pending"], "原因": b["pending_reason"]},
        "区域确认总数_不得为17": refreshed["region_confirmed_total"],
        "已对齐可核对原数": refreshed["aligned_package"]["original_figures"],
    }
    out_json = DATA_ROOT / "city_handoff_view.json"
    out_json.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    print("紧凑核对视图已写入：", out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
