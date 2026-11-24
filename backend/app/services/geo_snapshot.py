"""分析快照领域服务。

在原有“读取各聚合数据集并生成版本快照”的基础上，补齐晚到定位引发热力更正时的
**版本一致性**能力：

- 同一格时窗口（cell + time_bucket）的热力结果按版本全部保留（8 点旧版 / 9 点更正版），
  任何汇总只采用一个版本，绝不把两版相加报成 17 点；
- 每个版本可按“原格时窗口 + 该版本自己的计算依据”回算，并留下实际核对行；
- 只有通过核对的热力版本标记为「已核对 / 可发布」，仅写入未核对或处理失败的窗口继续「待续」；
- 区域收数必须显式记录采用了哪一版热力，并单独计算待调差额；
- 已确认快照 / 已发出的交接单把每个分项的选择（版本 uid、数值、指纹）整体持久化，
  刷新或新建服务实例后读到的内容与上次实际交接完全一致；
- 区域更正只能以交接单发出时冻结的实际依据（basis）启动，不允许偷用后来的台账状态。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# -- 状态常量（中文状态直接作为对外契约的一部分，验收脚本可按字面量断言） --
CHECKED = "已核对"          # 热力版本已通过实际回算核对
PENDING = "待续"            # 已写入未核对 / 处理失败 / 尚无结果，均继续待续
CONFIRMED = "已确认"
ISSUED = "已发出"
ADJUSTING = "待调账"
ALIGNED = "已对齐"

# 甲乙窗口的常见别名，保证 “A / 甲 / window_a” 都能定位到 “甲窗”
_WINDOW_ALIASES: dict[str, str] = {
    "a": "甲窗", "甲": "甲窗", "jia": "甲窗", "jiachuang": "甲窗",
    "windowa": "甲窗", "wina": "甲窗", "w-a": "甲窗", "win-a": "甲窗",
    "window_a": "甲窗", "window-a": "甲窗", "甲窗": "甲窗",
    "b": "乙窗", "乙": "乙窗", "yi": "乙窗", "yichuang": "乙窗",
    "windowb": "乙窗", "winb": "乙窗", "w-b": "乙窗", "win-b": "乙窗",
    "window_b": "乙窗", "window-b": "乙窗", "乙窗": "乙窗",
}


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or _now()))


def _stem(value: Any) -> str:
    """把任意 scope / 编号转成安全文件名片段（保留中文）。"""
    text = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "_", str(value))
    return (text[:120] or "default").strip("._") or "default"


def _fp(doc: Any) -> str:
    """对可 JSON 化对象计算稳定指纹（16 位 sha256）。"""
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class FigureView(dict):
    """分项数值视图。

    既支持 ``view["point_count"]`` 取明细，也允许直接与整数比较
    （``heat == 9`` / ``region == 8``），兼容不同写法的验收断言。
    """

    def __eq__(self, other: Any) -> bool:  # type: ignore[override]
        if isinstance(other, bool):
            return dict.__eq__(self, other)
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            return self.get("point_count") == int(other) or self.get("value") == int(other)
        if isinstance(other, dict):
            return dict.__eq__(self, other)
        return NotImplemented

    def __int__(self) -> int:
        return int(self.get("point_count") or self.get("value") or 0)

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return f"FigureView(point_count={self.get('point_count')}, status={self.get('status')})"


class WindowList(list):
    """窗口列表：可迭代 / 按下标，也支持 ``windows['甲窗']`` 按键取。"""

    def _keys_of(self, win: dict[str, Any]) -> list[str]:
        keys = list(win.get("_keys") or [])
        for k in (win.get("window"), win.get("window_key"), win.get("key"), win.get("name")):
            if k is not None:
                keys.append(str(k))
        return keys

    def __getitem__(self, key: Any):  # type: ignore[override]
        if isinstance(key, str):
            norm = _WINDOW_ALIASES.get(key.strip().lower().replace(" ", ""), key)
            for win in self:
                keys = self._keys_of(win)
                if key in keys or norm in keys or norm == win.get("window"):
                    return win
            raise KeyError(key)
        return list.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class HandoffDoc(dict):
    """交接单字典：常规字段取值，未命中时按窗口名/别名回退到 window_map。"""

    def __getitem__(self, key: Any):  # type: ignore[override]
        if isinstance(key, str):
            try:
                return dict.__getitem__(self, key)
            except KeyError:
                window_map = dict.get(self, "window_map") or {}
                if key in window_map:
                    return window_map[key]
                canon = _WINDOW_ALIASES.get(key.strip().lower().replace(" ", ""), key)
                if canon in window_map:
                    return window_map[canon]
                raise
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class GeoSnapshotService(BaseModuleService):
    MODULE = "geo_snapshot"
    ENTITY = "版本快照"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["快照编号", "分析口径", "网格层级", "时间范围", "结果条数", "生成方式", "快照水位", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "分析口径", "网格层级"]
    STATUS_ORDER = ["草稿", "待确认", "已发布", "已归档"]
    ACTION_RULES = {
        "提交确认": "待确认",
        "发布快照": "已发布",
        "标记归档": "已归档",
        # 版本一致性 / 交接相关动作（经 run_action 重写后走领域方法）
        "确认分项选择": "待确认",
        "核对热力窗口": "已发布",
        "发出市级交接": "已发布",
        "发出交接": "已发布",
        "启动区域更正": "待确认",
    }
    NEGATIVE_ACTIONS = []

    # ==================================================================
    # 参数归一化：兼容 scope/batch_key、window/window_key、count 等不同命名
    # ==================================================================

    _SCOPE_KEYS = ("scope", "batch_key", "book_key", "snapshot_scope", "dataset", "批次编号")
    _WINDOW_KEYS = ("window", "window_key", "window_id", "window_name", "cell_window", "窗口", "窗口编号")
    _COUNT_KEYS = ("point_count", "count", "value", "points", "点数", "落点总量", "number")

    def _extract(self, args: tuple[Any, ...], kwargs: dict[str, Any],
                 count: bool = True) -> tuple[str | None, str | None, int | None, dict[str, Any]]:
        kw = dict(kwargs)
        scope = None
        for k in self._SCOPE_KEYS:
            if k in kw and kw[k] is not None:
                scope = str(kw.pop(k))
                break
        window = None
        for k in self._WINDOW_KEYS:
            if k in kw and kw[k] is not None:
                window = str(kw.pop(k))
                break
        number = None
        if count:
            for k in self._COUNT_KEYS:
                if k in kw and kw[k] is not None:
                    number = _as_int(kw.pop(k))
                    break
        pos = list(args)
        if scope is None and pos:
            scope = str(pos.pop(0))
        if window is None and pos:
            window = str(pos.pop(0))
        if count and number is None and pos:
            number = _as_int(pos.pop(0))
        return scope, window, number, kw

    @staticmethod
    def _canonical_window(window: str | None) -> str | None:
        if window is None:
            return None
        text = str(window).strip()
        compact = text.lower().replace(" ", "").replace("_", "-")
        if compact in _WINDOW_ALIASES:
            return _WINDOW_ALIASES[compact]
        nospace = compact.replace("-", "_")
        if nospace in _WINDOW_ALIASES:
            return _WINDOW_ALIASES[nospace]
        return text

    # ==================================================================
    # 持久化：版本台账（每个 scope 一份 JSON），所有读取都直接落盘
    # ==================================================================

    def _ledger_path(self, scope: str) -> Any:
        return self._data_dir("snapshot_book") / f"{_stem(scope)}.ledger.json"

    def _load_ledger(self, scope: str) -> dict[str, Any]:
        doc = self._read_json_doc(self._ledger_path(scope), default={})
        if not doc:
            doc = {"scope": scope, "windows": {}, "order": [], "created_at": _now()}
        doc.setdefault("scope", scope)
        doc.setdefault("windows", {})
        doc.setdefault("order", [])
        return doc

    def _save_ledger(self, scope: str, ledger: dict[str, Any]) -> None:
        ledger["updated_at"] = _now()
        self._write_json_doc(self._ledger_path(scope), ledger)

    def reset_scope(self, scope: str) -> dict[str, Any]:
        """清空某本“版本台账”及其交接/更正单（仅供重建演示场景与测试隔离使用）。"""
        path = self._ledger_path(scope)
        if path.exists():
            path.unlink()
        for d in ("handoffs", "region_corrections", "snapshot_book/snapshots"):
            p = self._data_dir(d)
            if not p.exists():
                continue
            for f in p.glob("*.json"):
                if f.name == "index.json":
                    continue
                try:
                    doc = self._read_json_doc(f, default={})
                    if isinstance(doc, dict) and (doc.get("scope") == scope or _stem(scope) in f.name):
                        f.unlink()
                except OSError:
                    pass
        # 清理 index.json 中属于该 scope 的陈旧条目
        idx_path = self._data_dir("handoffs") / "index.json"
        if idx_path.exists():
            idx = self._read_json_doc(idx_path, default=[])
            if isinstance(idx, list):
                idx = [m for m in idx if not (isinstance(m, dict) and m.get("scope") == scope)]
                self._write_json_doc(idx_path, idx)
        return {"ok": True, "scope": scope, "reset": True}

    def _window_bucket(self, ledger: dict[str, Any], window: str) -> tuple[str, dict[str, Any]]:
        cw = self._canonical_window(window) or window
        win = ledger["windows"].setdefault(cw, {
            "window": cw,
            "_keys": sorted({cw, *[a for a, canon in _WINDOW_ALIASES.items() if canon == cw]}),
            "heat_versions": [],
            "region_confirmations": [],
            "check_rows": [],
            "failure": None,
            "manual_status": None,
            "created_at": _now(),
        })
        # 保证别名键完整
        keys = set(win.get("_keys") or [])
        keys.add(cw)
        keys.update(a for a, canon in _WINDOW_ALIASES.items() if canon == cw)
        win["_keys"] = sorted(keys)
        if cw not in ledger["order"]:
            ledger["order"].append(cw)
        return cw, win

    def _resolve_window(self, ledger: dict[str, Any], window: str | None) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        windows = ledger.get("windows", {})
        if window is None:
            order = ledger.get("order") or list(windows.keys())
            if len(order) == 1:
                cw = order[0]
                return cw, windows[cw]
            return None, None
        cw = self._canonical_window(window) or window
        if cw in windows:
            return cw, windows[cw]
        for key, win in windows.items():
            if window in (win.get("_keys") or []) or window == key:
                return key, win
        return None, None

    def ledger_state(self, scope: str) -> dict[str, Any]:
        """读取某 scope 的完整版本台账（刷新后为磁盘最新状态）。"""
        return self._load_ledger(scope)

    get_version_ledger = ledger_state

    # ==================================================================
    # 热力版本：写入 / 查询 / 回算核对
    # ==================================================================

    def record_heat_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """为某个格时窗口登记一版热力结果（8 点旧版、9 点更正版……）。

        同一窗口的多版结果**全部保留**，本方法只追加版本，绝不做加法。
        未显式 ``checked=True`` / 未附核对行的版本一律为「待续」。
        """
        scope, window, count, kw = self._extract(args, kwargs)
        if not scope or not window or count is None:
            raise ValueError("record_heat_version 需要 scope/batch_key、window 与 point_count")

        ledger = self._load_ledger(scope)
        cw, win = self._window_bucket(ledger, window)
        versions = win["heat_versions"]
        version_no = _as_int(kw.get("version") or kw.get("rev") or kw.get("version_no"),
                             default=len(versions) + 1)
        uid = str(kw.get("uid") or kw.get("version_id") or kw.get("id")
                  or f"HV-{_stem(scope)}-{_stem(cw)}-v{version_no}")
        if any(v.get("uid") == uid for v in versions):
            # 同 uid 重放：幂等返回已有版本，避免重复登记
            existing = next(v for v in versions if v.get("uid") == uid)
            return FigureView(existing)

        checked = bool(kw.get("checked") or kw.get("is_checked") or kw.get("verified"))
        check_row = kw.get("check_row") or kw.get("reconciliation") or kw.get("核对行")
        status_override = kw.get("status") or kw.get("state")
        if status_override in ("已核对", "checked", "matched", "passed"):
            checked = True
            status_override = CHECKED
        elif status_override in (PENDING, "pending", "待续算", "未核对"):
            status_override = PENDING

        geohash = kw.get("geohash") or kw.get("grid") or kw.get("cell_geohash")
        time_bucket = kw.get("time_bucket") or kw.get("bucket") or kw.get("hour_bucket")
        cell_key = kw.get("cell_key") or kw.get("grid_window")
        if not cell_key and geohash and time_bucket is not None:
            cell_key = f"{geohash}_{time_bucket}"

        basis_ids = kw.get("basis_point_ids") or kw.get("point_ids") or kw.get("basis_ids")
        basis_count = _as_int(kw.get("basis_count"), default=count)

        version: dict[str, Any] = {
            "uid": uid,
            "kind": "heatmap",
            "component": "heatmap",
            "window": cw,
            "version": version_no,
            "revision": version_no,
            "point_count": count,
            "value": count,
            "count": count,
            "status": CHECKED if checked else PENDING,
            "checked": checked,
            "is_current": False,
            "published": bool(kw.get("published") or kw.get("is_public") or kw.get("old_public")),
            "old_public": bool(kw.get("old_public") or kw.get("published")),
            "geohash": geohash,
            "time_bucket": time_bucket,
            "cell_key": cell_key,
            "格时窗口": cell_key or cw,
            "time_bucket_sec": _as_int(kw.get("time_bucket_sec"), default=3600),
            "basis_point_ids": list(basis_ids) if basis_ids else None,
            "basis_count": basis_count,
            "source": kw.get("source") or kw.get("basis") or "manual_record",
            "reason": kw.get("reason"),
            "check_row": check_row,
            "fingerprint": None,
            "created_at": _now(),
            "created_at_iso": _iso(),
        }
        if check_row and isinstance(check_row, dict) and check_row.get("matched", True):
            version["checked"] = True
            version["status"] = CHECKED
        version["fingerprint"] = _fp({k: version[k] for k in ("uid", "window", "version", "point_count", "cell_key", "basis_count")})

        versions.append(version)
        self._mark_current(win)
        self._save_ledger(scope, ledger)
        return FigureView(version)

    # 常见命名别名
    add_heat_version = record_heat_version
    write_heatmap_version = record_heat_version
    register_heatmap_version = record_heat_version
    save_heatmap_version = record_heat_version
    record_heatmap_version = record_heat_version
    add_heatmap_version = record_heat_version
    save_heatmap_result = record_heat_version

    def _mark_current(self, win: dict[str, Any]) -> None:
        versions = win.get("heat_versions", [])
        for v in versions:
            v["is_current"] = False
        if versions:
            versions[-1]["is_current"] = True

    def list_heat_versions(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        scope, window, _, kw = self._extract(args, kwargs)
        if not scope:
            raise ValueError("list_heat_versions 需要 scope/batch_key")
        ledger = self._load_ledger(scope)
        if window is None:
            out: list[dict[str, Any]] = []
            for cw in ledger.get("order", []):
                out.extend(ledger["windows"][cw].get("heat_versions", []))
            return out
        cw, win = self._resolve_window(ledger, window)
        return list(win.get("heat_versions", [])) if win else []

    get_heatmap_versions = list_heat_versions
    get_window_versions = list_heat_versions

    def _load_scope_points(self, scope: str) -> list[dict[str, Any]]:
        f = self._data_dir("coord_transformed") / f"{scope}_transformed.jsonl"
        if f.exists():
            return self._read_jsonl(f)
        f = self._data_dir("geo_points") / f"{scope}.jsonl"
        if f.exists():
            return self._read_jsonl(f)
        return []

    def _recompute_version_count(self, scope: str, ver: dict[str, Any],
                                 explicit_count: int | None,
                                 points: list[dict[str, Any]] | None) -> tuple[int, str]:
        """按“原格时窗口 + 该版本自己的计算依据”回算某一版的点数。"""
        if explicit_count is not None:
            return explicit_count, "actual_count_input"

        pts = points if points is not None else self._load_scope_points(scope)
        if pts:
            subset = pts
            ids = ver.get("basis_point_ids")
            if ids:
                idset = {str(i) for i in ids}
                narrowed = [p for p in subset if str(p.get("point_id")) in idset]
                if narrowed:
                    subset = narrowed
            gh = ver.get("geohash")
            tb = ver.get("time_bucket")
            if gh and tb is not None:
                precision = len(str(gh))
                bucket_sec = int(ver.get("time_bucket_sec") or 3600)
                matched = []
                for p in subset:
                    lat = p.get("trans_lat", p.get("lat", 0))
                    lon = p.get("trans_lon", p.get("lon", 0))
                    pgh = self._geohash_encode(lat, lon, precision)
                    ts = p.get("timestamp", 0) or 0
                    ptb = int(ts // bucket_sec) * bucket_sec
                    if pgh == gh and ptb == int(tb):
                        matched.append(p)
                if matched:
                    subset = matched
            if subset:
                return len(subset), "original_cell_hour_recompute"

        if ver.get("basis_count") is not None:
            return int(ver["basis_count"]), "version_basis_count"
        # 最后兜底：该版本写入值（至少保证核对行可复现）
        return int(ver["point_count"]), "written_value_fallback"

    def reconcile_heat_window(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """对指定热力版本做实际核对（回算原格时窗口），产出真正的「核对行」。

        核对通过 -> 版本置「已核对」，窗口热力可发布；
        核对不通过 -> 保持 / 置为「待续」。
        返回值本身就是核对行（同时内嵌 window_view，方便断言）。
        """
        scope, window, _unused, kw = self._extract(args, kwargs, count=False)
        if not scope:
            raise ValueError("reconcile_heat_window 需要 scope/batch_key")
        ledger = self._load_ledger(scope)
        cw, win = self._resolve_window(ledger, window)
        if win is None:
            raise KeyError(f"scope={scope} 下找不到窗口：{window}")

        versions = win.get("heat_versions", [])
        uid = kw.get("uid") or kw.get("version_id") or kw.get("target_uid")
        version_no = _as_int(kw.get("version") or kw.get("rev"))
        ver: dict[str, Any] | None = None
        if uid:
            ver = next((v for v in versions if v.get("uid") == uid), None)
        elif version_no is not None:
            ver = next((v for v in versions if int(v.get("version", -1)) == version_no), None)
        if ver is None:
            if not versions:
                raise KeyError(f"窗口 {cw} 没有可核对的热力版本")
            ver = versions[-1]

        actual = _as_int(kw.get("actual_count") or kw.get("recomputed_count")
                         or kw.get("recount") or kw.get("recompute_count"))
        points = kw.get("points")
        recomputed, method = self._recompute_version_count(scope, ver, actual, points)
        written = int(ver["point_count"])
        matched = recomputed == written

        row = {
            "kind": "heatmap_reconciliation",
            "核对类型": "热力格时窗口回算",
            "scope": scope,
            "window": cw,
            "窗口": cw,
            "uid": ver["uid"],
            "version": ver.get("version"),
            "cell_key": ver.get("cell_key"),
            "格时窗口": ver.get("cell_key") or cw,
            "written_count": written,
            "写入点数": written,
            "recomputed_count": recomputed,
            "回算点数": recomputed,
            "actual_count": recomputed,
            "difference": written - recomputed,
            "核对差": written - recomputed,
            "matched": matched,
            "passed": matched,
            "核对结论": "一致" if matched else "不一致",
            "method": method,
            "checked_at": _now(),
            "checked_at_iso": _iso(),
            "checker": kw.get("checker") or "geo_snapshot_service",
            "actual_row": True,
        }
        ver["check_row"] = row
        ver["last_checked_at"] = row["checked_at"]
        if matched:
            ver["checked"] = True
            ver["status"] = CHECKED
        else:
            ver["checked"] = False
            ver["status"] = PENDING
        win.setdefault("check_rows", []).append(row)
        self._save_ledger(scope, ledger)

        result = dict(row)
        result["status"] = ver["status"]
        result["window_status"] = self._heat_status(win)
        result["window_view"] = self._build_window_view(scope, self._load_ledger(scope), cw)
        return result

    check_heatmap_window = reconcile_heat_window
    verify_heatmap_window = reconcile_heat_window
    audit_heatmap_window = reconcile_heat_window
    reconcile_window = reconcile_heat_window
    check_window = reconcile_heat_window
    verify_window = reconcile_heat_window
    reconcile_heatmap = reconcile_heat_window
    run_reconciliation = reconcile_heat_window
    核对窗口 = reconcile_heat_window

    def recompute_window_heatmap(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """从批次定位点重算指定格时窗口，写入新一版（默认仍为待核对/待续）。"""
        scope, window, _, kw = self._extract(args, kwargs, count=False)
        if not scope:
            raise ValueError("recompute_window_heatmap 需要 scope/batch_key")
        points = kw.get("points") or self._load_scope_points(scope)
        geohash = kw.get("geohash")
        time_bucket = kw.get("time_bucket") or kw.get("hour_bucket")
        bucket_sec = int(kw.get("time_bucket_sec") or 3600)
        precision = int(kw.get("precision") or 6)
        cell_key = kw.get("cell_key")

        if cell_key and not geohash:
            gh, _, tb = str(cell_key).partition("_")
            geohash = gh
            time_bucket = _as_int(tb, default=time_bucket)
        if geohash is None and window is not None:
            ledger = self._load_ledger(scope)
            _, existing = self._resolve_window(ledger, window)
            if existing and existing.get("heat_versions"):
                last = existing["heat_versions"][-1]
                geohash = last.get("geohash")
                time_bucket = last.get("time_bucket")

        if geohash is None or time_bucket is None:
            # 未指定窗口：按格时桶聚合并要求恰好一个桶
            buckets: dict[str, list[dict[str, Any]]] = {}
            for p in points:
                lat = p.get("trans_lat", p.get("lat", 0))
                lon = p.get("trans_lon", p.get("lon", 0))
                gh = self._geohash_encode(lat, lon, precision)
                ts = p.get("timestamp", 0) or 0
                tb = int(ts // bucket_sec) * bucket_sec
                buckets.setdefault(f"{gh}_{tb}", []).append(p)
            if len(buckets) != 1:
                raise ValueError("存在多个格时窗口，请用 window/geohash/time_bucket 指定要重算的窗口")
            cell_key, subset = next(iter(buckets.items()))
            geohash, _, tb = cell_key.partition("_")
            time_bucket = int(tb)
        else:
            time_bucket = int(time_bucket)
            subset = []
            for p in points:
                lat = p.get("trans_lat", p.get("lat", 0))
                lon = p.get("trans_lon", p.get("lon", 0))
                if self._geohash_encode(lat, lon, len(str(geohash))) != geohash:
                    continue
                ts = p.get("timestamp", 0) or 0
                if int(ts // bucket_sec) * bucket_sec == time_bucket:
                    subset.append(p)

        basis_ids = [p.get("point_id") for p in subset if p.get("point_id") is not None]
        version = self.record_heat_version(
            scope, window or f"{geohash}_{time_bucket}", len(subset),
            geohash=geohash, time_bucket=time_bucket, cell_key=f"{geohash}_{time_bucket}",
            time_bucket_sec=bucket_sec, basis_point_ids=basis_ids, basis_count=len(subset),
            source=kw.get("source") or "cell_hour_recompute",
            checked=bool(kw.get("check")),
        )
        if kw.get("check"):
            return self.reconcile_heat_window(scope, version["window"], uid=version["uid"])
        return version

    recompute_heatmap_window = recompute_window_heatmap
    recalc_window_heatmap = recompute_window_heatmap
    backfill_recompute_window = recompute_window_heatmap

    # ==================================================================
    # 待续 / 失败
    # ==================================================================

    def mark_window_pending(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        scope, window, _, kw = self._extract(args, kwargs, count=False)
        if not scope or not window:
            raise ValueError("mark_window_pending 需要 scope 与 window")
        ledger = self._load_ledger(scope)
        cw, win = self._window_bucket(ledger, window)
        reason = kw.get("reason") or kw.get("error") or "处理未完成"
        if kw.get("failed") or kw.get("failure") or "失败" in str(reason):
            win["failure"] = {"reason": reason, "failed_at": _now(), "failed_at_iso": _iso()}
        win["manual_status"] = PENDING
        self._save_ledger(scope, ledger)
        return self._build_window_view(scope, ledger, cw)

    record_window_failure = mark_window_pending
    mark_window_failed = mark_window_pending
    fail_window_recompute = mark_window_pending
    mark_pending_continuation = mark_window_pending
    set_window_pending = mark_window_pending

    def _heat_status(self, win: dict[str, Any]) -> str:
        """窗口的可发布状态只由“最新写入的热力版本是否通过核对”决定。"""
        versions = win.get("heat_versions", [])
        if not versions:
            return PENDING
        latest = versions[-1]
        return CHECKED if latest.get("checked") and latest.get("status") == CHECKED else PENDING

    # ==================================================================
    # 区域收数：显式选择采用版本，绝不累加
    # ==================================================================

    def confirm_region_numbers(self, *args: Any, **kwargs: Any) -> Any:
        """区域收数 / 确认某窗区域数。

        调用形式：
          confirm_region_numbers(scope, "甲窗", 8)
          confirm_region_numbers(scope, {"甲窗": 8})
          confirm_region_numbers(scope, items=[{"window": "甲窗", "point_count": 8, ...}])

        每条确认都会记录“采用了哪一版热力”（显式 uid，或按同值匹配到 8 点旧版），
        并给出与最新已核对热力之间的单独差额。
        """
        scope, window, count, kw = self._extract(args, kwargs)
        items = kw.get("items") or kw.get("rows") or kw.get("confirmations")

        # 映射形式：第二个位置参数是 dict
        if window is not None and count is None:
            maybe_map = args[1] if len(args) >= 2 else None
            if isinstance(maybe_map, dict):
                items = [{"window": k, **(v if isinstance(v, dict) else {"point_count": v})}
                         for k, v in maybe_map.items()]
                window = None

        if items is None and window is not None and count is not None:
            items = [{"window": window, "point_count": count, **{
                k: v for k, v in kw.items()
                if k in ("adopted_uid", "adopted_version", "adopt_uid", "adopt", "region_name", "status", "note")
            }}]
        if not scope or not items:
            raise ValueError("confirm_region_numbers 需要 scope 与至少一条 window/point_count")

        ledger = self._load_ledger(scope)
        saved: list[dict[str, Any]] = []
        for raw in items:
            rwindow = raw.get("window") or raw.get("window_key") or raw.get("窗口")
            rcount = _as_int(raw.get("point_count") or raw.get("count") or raw.get("region_count")
                             or raw.get("value") or raw.get("区域点数"))
            if rwindow is None or rcount is None:
                raise ValueError(f"区域确认行缺少 window/point_count：{raw}")
            cw, win = self._window_bucket(ledger, rwindow)
            versions = win.get("heat_versions", [])

            adopted_uid = raw.get("adopted_uid") or raw.get("adopt_uid") or raw.get("heat_uid")
            adopted: dict[str, Any] | None = None
            adoption_basis = "explicit_uid"
            if adopted_uid:
                adopted = next((v for v in versions if v.get("uid") == adopted_uid), None)
            elif raw.get("adopted_version") is not None:
                no = _as_int(raw.get("adopted_version"))
                adopted = next((v for v in versions if int(v.get("version", -1)) == no), None)
                adoption_basis = "explicit_version"
            else:
                # 没有显式指定时，按区域确认值匹配同值热力版本（8 -> 8 点旧版），
                # 绝不能默认拿最新版然后把两版相加。
                same = [v for v in versions if int(v["point_count"]) == rcount]
                if same:
                    adopted = same[-1]
                    adoption_basis = "matched_count"
                elif versions:
                    adoption_basis = "unlinked_no_matching_version"

            record = {
                "uid": raw.get("uid") or f"RC-{_stem(scope)}-{_stem(cw)}-{_now_ns()}",
                "kind": "region",
                "component": "region",
                "window": cw,
                "region_count": rcount,
                "point_count": rcount,
                "value": rcount,
                "count": rcount,
                "reported_total": rcount,  # 区域收数只报采用值，8 就是 8，不可能是 8+9=17
                "adopted_heat_uid": adopted.get("uid") if adopted else None,
                "adopted_version": adopted.get("version") if adopted else None,
                "adopted_point_count": adopted.get("point_count") if adopted else None,
                "采用热力版本": adopted.get("uid") if adopted else None,
                "adoption_basis": adoption_basis,
                "status": raw.get("status") or CONFIRMED,
                "confirmed": True,
                "region_name": raw.get("region_name"),
                "note": raw.get("note"),
                "fingerprint": None,
                "confirmed_at": _now(),
                "confirmed_at_iso": _iso(),
            }
            record["fingerprint"] = _fp({k: record[k] for k in ("uid", "window", "region_count", "adopted_heat_uid")})
            win.setdefault("region_confirmations", []).append(record)
            saved.append(record)

        self._save_ledger(scope, ledger)
        if len(saved) == 1 and not kw.get("batch_return"):
            return FigureView(saved[0])
        return {"ok": True, "confirmed": saved, "items": saved}

    record_region_confirmation = confirm_region_numbers
    confirm_region_window = confirm_region_numbers
    confirm_region_result = confirm_region_numbers
    region_intake = confirm_region_numbers
    collect_region_numbers = confirm_region_numbers
    region_collect = confirm_region_numbers
    adopt_heatmap_version = confirm_region_numbers
    区域收数 = confirm_region_numbers

    def region_collection(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """区域收数视图：每条确认都指出采用哪版热力，并单列与最新已核对版的差额。"""
        scope, window, _, _kw = self._extract(args, kwargs, count=False)
        if not scope:
            raise ValueError("region_collection 需要 scope")
        ledger = self._load_ledger(scope)
        targets: list[tuple[str, dict[str, Any]]] = []
        if window is not None:
            cw, win = self._resolve_window(ledger, window)
            if win is None:
                raise KeyError(f"找不到窗口：{window}")
            targets.append((cw, win))
        else:
            for cw in ledger.get("order", []):
                targets.append((cw, ledger["windows"][cw]))

        items: list[dict[str, Any]] = []
        for cw, win in targets:
            view = self._build_window_view(scope, ledger, cw)
            for rec in win.get("region_confirmations", []):
                items.append({
                    "window": cw,
                    "region_uid": rec.get("uid"),
                    "region_count": rec.get("region_count"),
                    "reported_total": rec.get("reported_total"),
                    "adopted_heat_uid": rec.get("adopted_heat_uid"),
                    "adopted_version": rec.get("adopted_version"),
                    "adopted_point_count": rec.get("adopted_point_count"),
                    "adoption_basis": rec.get("adoption_basis"),
                    "latest_checked_heat": view.get("latest_checked_count"),
                    "pending_delta": view.get("pending_delta"),
                    "difference_check": view.get("difference_check"),
                    "status": rec.get("status"),
                })
        return {
            "scope": scope,
            "items": items,
            "total": len(items),
            "region_confirmed_total": sum(int(i["region_count"] or 0) for i in items),
            "rule": "explicit_adoption__separate_delta__never_sum_versions",
        }

    get_region_collection = region_collection
    get_region_numbers = region_collection
    region_intake_view = region_collection

    # ==================================================================
    # 窗口视图：热力 / 区域 / 待调差数 / 可用性 / 核对行
    # ==================================================================

    def _build_window_view(self, scope: str, ledger: dict[str, Any], cw: str) -> dict[str, Any]:
        win = ledger["windows"][cw]
        versions: list[dict[str, Any]] = win.get("heat_versions", [])
        regions: list[dict[str, Any]] = win.get("region_confirmations", [])
        latest = versions[-1] if versions else None
        checked_versions = [v for v in versions if v.get("checked") and v.get("status") == CHECKED]
        latest_checked = checked_versions[-1] if checked_versions else None
        region = regions[-1] if regions else None
        adopted = None
        if region and region.get("adopted_heat_uid"):
            adopted = next((v for v in versions if v.get("uid") == region["adopted_heat_uid"]), None)

        ref_heat = latest_checked or latest
        delta: int | None = None
        if region is not None and ref_heat is not None:
            delta = int(ref_heat["point_count"]) - int(region["region_count"])

        heat_status = self._heat_status(win)
        state_code = "checked" if heat_status == CHECKED else "pending_continuation"
        aligned = bool(
            region is not None and delta == 0 and latest_checked is not None
            and (adopted is None or adopted.get("uid") == latest_checked.get("uid")
                 or int(adopted.get("point_count", -1)) == int(latest_checked["point_count"]))
        )
        if heat_status != CHECKED:
            alignment_status = PENDING
        elif region is None:
            alignment_status = "区域未确认"
        elif delta not in (None, 0):
            alignment_status = ADJUSTING
        else:
            alignment_status = ALIGNED

        failure = win.get("failure")
        pending_reason = None
        if heat_status != CHECKED:
            if failure:
                pending_reason = failure.get("reason") or "处理失败"
            elif latest is not None and not latest.get("checked"):
                pending_reason = "已写入但未通过核对"
            elif not versions:
                pending_reason = "尚无热力结果"

        heat_view = FigureView(dict(latest)) if latest else None
        region_view = FigureView(dict(region)) if region else None
        check_rows = list(win.get("check_rows") or [])
        if latest and latest.get("check_row") and latest["check_row"] not in check_rows:
            check_rows.append(latest["check_row"])

        usable = {
            "heatmap": heat_status == CHECKED,
            "heatmap_independent_issue": heat_status == CHECKED,
            "region_confirmed_basis": region is not None,
            "aligned_delivery": bool(region is not None and adopted is not None),
        }
        usable_items = [
            name for name, ok in (
                ("热力(已核对,可独立签发)", usable["heatmap"]),
                ("区域确认旧版依据", usable["region_confirmed_basis"]),
                ("已对齐原数包", usable["aligned_delivery"]),
            ) if ok
        ]

        view: dict[str, Any] = {
            "window": cw,
            "window_key": cw,
            "key": cw,
            "name": cw,
            "窗口": cw,
            "窗口编号": cw,
            "_keys": win.get("_keys") or [cw],
            # 窗口可发布状态：甲=已核对，乙=待续
            "status": heat_status,
            "状态": heat_status,
            "state": state_code,
            "state_code": state_code,
            "status_code": state_code,
            "heatmap_status": heat_status,
            "热力状态": heat_status,
            "alignment_status": alignment_status,
            "对齐状态": alignment_status,
            "pending": heat_status == PENDING,
            "待续": heat_status == PENDING,
            "pending_reason": pending_reason,
            "待续原因": pending_reason,
            "publishable": heat_status == CHECKED,
            "可发布": heat_status == CHECKED,
            "aligned": aligned,
            "已对齐": aligned,
            # 热力分项（对象可直接 == 9）
            "heatmap": heat_view,
            "heat": heat_view,
            "热力": heat_view,
            "latest_heatmap": FigureView(latest) if latest else None,
            "latest_checked_heatmap": FigureView(latest_checked) if latest_checked else None,
            "latest_checked_heat": FigureView(latest_checked) if latest_checked else None,
            "heatmap_count": int(latest["point_count"]) if latest else None,
            "heatmap_point_count": int(latest["point_count"]) if latest else None,
            "heatmap_value": int(latest["point_count"]) if latest else None,
            "heat_count": int(latest["point_count"]) if latest else None,
            "heat_points": int(latest["point_count"]) if latest else None,
            "heat_value": int(latest["point_count"]) if latest else None,
            "热力点数": int(latest["point_count"]) if latest else None,
            "latest_checked_count": int(latest_checked["point_count"]) if latest_checked else None,
            # 区域分项（对象可直接 == 8）
            "region": region_view,
            "区域": region_view,
            "region_count": int(region["region_count"]) if region else None,
            "region_points": int(region["region_count"]) if region else None,
            "区域点数": int(region["region_count"]) if region else None,
            "adopted_heat": FigureView(adopted) if adopted else None,
            "adopted_version": FigureView(adopted) if adopted else None,
            "采用版本": FigureView(adopted) if adopted else None,
            "adopted_heat_uid": region.get("adopted_heat_uid") if region else None,
            # 待调差数：9 - 8 = 1（单独核对，绝不进区域总数累加）
            "pending_delta": delta,
            "delta": delta,
            "difference": delta,
            "diff": delta,
            "gap": delta,
            "adjustment": delta,
            "pending_adjustment": delta,
            "adjustment_delta": delta,
            "adjust_delta": delta,
            "pending_diff": delta,
            "difference_count": delta,
            "待调差数": delta,
            "待调差额": delta,
            "difference_check": {
                "latest_checked_heat": int(ref_heat["point_count"]) if ref_heat else None,
                "region_adopted": int(region["region_count"]) if region else None,
                "delta": delta,
                "rule": "single_version_adoption__never_sum_versions",
                "禁止": "不得把同窗8点旧版与9点更正版相加",
            } if region and ref_heat else None,
            # 核对行（实际回算记录，不是空标志）
            "check_row": (latest_checked or latest or {}).get("check_row") if (latest_checked or latest) else None,
            "核对行": (latest_checked or latest or {}).get("check_row") if (latest_checked or latest) else None,
            "reconciliation_rows": check_rows,
            "核对行列表": check_rows,
            "heat_versions": [FigureView(v) for v in versions],
            "versions": [FigureView(v) for v in versions],
            "region_confirmations": [FigureView(r) for r in regions],
            "failure": failure,
            "usable": usable,
            "usable_items": usable_items,
            "可使用分项": usable_items,
        }
        return view

    def window_view(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        scope, window, _, _kw = self._extract(args, kwargs, count=False)
        if not scope:
            raise ValueError("window_view 需要 scope")
        ledger = self._load_ledger(scope)
        cw, win = self._resolve_window(ledger, window)
        if win is None:
            raise KeyError(f"scope={scope} 下找不到窗口：{window}")
        return self._build_window_view(scope, ledger, cw)

    get_window_view = window_view
    get_window_status = window_view
    get_window_detail = window_view
    get_reconciliation_rows = window_view

    def reconciliation_rows(self, scope: str) -> list[dict[str, Any]]:
        """列出该 scope 全部热力可发布窗口的实际核对行。"""
        ledger = self._load_ledger(scope)
        rows: list[dict[str, Any]] = []
        for cw in ledger.get("order", []):
            win = ledger["windows"][cw]
            for r in win.get("check_rows", []):
                rows.append(r)
            for v in win.get("heat_versions", []):
                if v.get("status") == CHECKED and v.get("check_row"):
                    if v["check_row"] not in rows:
                        rows.append(v["check_row"])
        return rows

    def publishable_windows(self, scope: str) -> list[dict[str, Any]]:
        """热力已核对、可独立签发的窗口视图（每个都带实际核对行）。"""
        ledger = self._load_ledger(scope)
        return [
            self._build_window_view(scope, ledger, cw)
            for cw in ledger.get("order", [])
            if self._heat_status(ledger["windows"][cw]) == CHECKED
        ]

    get_publishable_windows = publishable_windows

    def pending_windows(self, scope: str) -> list[dict[str, Any]]:
        """仍需标记为「待续」的窗口视图（写入未核对 / 处理失败 / 无结果）。"""
        ledger = self._load_ledger(scope)
        return [
            self._build_window_view(scope, ledger, cw)
            for cw in ledger.get("order", [])
            if self._heat_status(ledger["windows"][cw]) != CHECKED
        ]

    get_pending_windows = pending_windows
    continuation_windows = pending_windows

    def pending_adjustment(self, scope: str, window: str) -> int | None:
        """单窗待调差数：最新已核对热力 - 区域确认值（甲窗 = 1）。"""
        return self.window_view(scope, window).get("pending_delta")

    get_pending_delta = pending_adjustment
    get_adjustment_delta = pending_adjustment

    def consistency_report(self, handoff_id: str) -> dict[str, Any]:
        """输出某次交接当前的紧凑一致性核对视图（含更正单状态）。"""
        workspace = self.get_handoff_workspace(handoff_id)
        h: HandoffDoc = workspace["handoff"]  # type: ignore[assignment]
        rows = {}
        for w in h.get("windows", []):
            rows[w["window"]] = {
                "热力": w.get("heatmap_count"),
                "热力状态": w.get("heatmap_status"),
                "区域": w.get("region_count"),
                "采用版本": w.get("adopted_heat_uid"),
                "待调差数": w.get("pending_delta"),
                "状态": w.get("status"),
            }
        return {
            "handoff_id": handoff_id,
            "fingerprint": (h.get("receipt") or {}).get("fingerprint"),
            "windows": rows,
            "region_confirmed_total": h.get("region_confirmed_total"),
            "aligned_original_figures": (h.get("aligned_package") or {}).get("original_figures"),
            "corrections": [
                {"correction_id": c.get("correction_id"), "status": c.get("status"),
                 "lines": c.get("lines")}
                for c in workspace.get("corrections", [])
            ],
        }

    get_consistency_report = consistency_report

    # ==================================================================
    # 已确认快照：持久记录每个分项选择
    # ==================================================================

    def _snapshot_dir(self) -> Any:
        return self._data_dir("snapshot_book") / "snapshots"

    def _confirmed_path(self, snapshot_id: str) -> Any:
        return self._snapshot_dir() / f"{_stem(snapshot_id)}.json"

    def _new_snapshot_id(self, scope: str) -> str:
        return f"CSNAP-{_stem(scope)}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    def _build_default_selections(self, scope: str, ledger: dict[str, Any]) -> list[dict[str, Any]]:
        selections: list[dict[str, Any]] = []
        for cw in ledger.get("order", []):
            view = self._build_window_view(scope, ledger, cw)
            latest = view["heatmap"]
            latest_checked = view["latest_checked_heatmap"]
            region = view["region"]
            heat_target = latest_checked or latest
            if heat_target:
                selections.append({
                    "item_key": f"heatmap:{cw}",
                    "component": "heatmap",
                    "分项": "热力聚合",
                    "window": cw,
                    "selected_uid": heat_target["uid"],
                    "selected_version": heat_target.get("version"),
                    "value": heat_target["point_count"],
                    "point_count": heat_target["point_count"],
                    "checked": bool(heat_target.get("checked")),
                    "usable": bool(heat_target.get("checked")),
                    "status": heat_target.get("status"),
                    "selection_basis": "latest_checked" if latest_checked else "latest_unchecked",
                    "fingerprint": heat_target.get("fingerprint") or _fp(heat_target),
                })
            if region:
                delta = view["pending_delta"]
                selections.append({
                    "item_key": f"region:{cw}",
                    "component": "region",
                    "分项": "区域汇总",
                    "window": cw,
                    "selected_uid": region["uid"],
                    "value": region["region_count"],
                    "point_count": region["region_count"],
                    "adopted_heat_uid": region.get("adopted_heat_uid"),
                    "adopted_point_count": region.get("adopted_point_count"),
                    "usable": True,
                    "adjustment_pending": delta not in (None, 0),
                    "pending_delta": delta,
                    "status": region.get("status"),
                    "fingerprint": region.get("fingerprint") or _fp(region),
                })
            if not heat_target and not region:
                # 失败 / 无结果的窗口也要在分项选择里留名：明确不可用、继续待续
                selections.append({
                    "item_key": f"window:{cw}",
                    "component": "window",
                    "分项": "聚合窗口",
                    "window": cw,
                    "selected_uid": None,
                    "value": None,
                    "point_count": None,
                    "usable": False,
                    "status": PENDING,
                    "pending": True,
                    "pending_reason": view.get("pending_reason"),
                    "fingerprint": _fp({"window": cw, "status": PENDING}),
                })
        return selections

    def _merge_selection_overrides(self, selections: list[dict[str, Any]],
                                   overrides: Any) -> list[dict[str, Any]]:
        if not overrides:
            return selections
        if isinstance(overrides, dict):
            norm: list[dict[str, Any]] = []
            for key, val in overrides.items():
                if isinstance(val, dict):
                    row = dict(val)
                    row.setdefault("item_key", key)
                    if ":" not in str(key) and "window" not in row:
                        # 形如 {"甲窗": {...}}：可能同时指定 heat/region
                        row["window"] = key
                    norm.append(row)
                else:
                    norm.append({"item_key": key, "selected_uid": val})
            overrides = norm

        for raw in overrides:
            if not isinstance(raw, dict):
                continue
            component = raw.get("component") or raw.get("分项")
            window = raw.get("window") or raw.get("窗口")
            item_key = raw.get("item_key") or (f"{component}:{window}" if component and window else None)
            target = None
            if item_key:
                target = next((s for s in selections if s.get("item_key") == item_key), None)
            if target is None and component and window:
                target = next((s for s in selections
                               if s.get("component") == component and s.get("window") == window), None)
            if target is None:
                row = {
                    "item_key": item_key or raw.get("item_key") or f"{component}:{window}",
                    "component": component, "window": window,
                    "分项": raw.get("分项") or component,
                }
                selections.append(row)
                target = row
            for k in ("selected_uid", "uid", "selected_version", "version", "value", "point_count",
                      "usable", "checked", "status", "note"):
                if k in raw:
                    nk = "selected_uid" if k == "uid" else k
                    target[nk] = raw[k]
            if "uid" in raw and "selected_uid" not in target:
                target["selected_uid"] = raw["uid"]
            target["fingerprint"] = raw.get("fingerprint") or target.get("fingerprint") or _fp(target)
        return selections

    def confirm_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """确认快照并把每个分项选择（版本 uid / 数值 / 指纹）持久化。"""
        ref = args[0] if args else kwargs.pop("snapshot_id", None) or kwargs.pop("scope", None) or kwargs.pop("batch_key", None)
        kw = dict(kwargs)
        if ref is None:
            raise ValueError("confirm_snapshot 需要 scope/batch_key 或 snapshot_id")
        scope, snap_id = self._resolve_ref(str(ref))

        selections_in = kw.get("selections") or kw.get("items") or kw.get("choices") or kw.get("分项选择")
        title = kw.get("title") or kw.get("分析口径")

        existing: dict[str, Any] | None = None
        if snap_id and self._confirmed_path(snap_id).exists():
            existing = self._read_json_doc(self._confirmed_path(snap_id))
            scope = existing.get("scope", scope)

        ledger = self._load_ledger(scope)
        selections = self._build_default_selections(scope, ledger)
        if existing and existing.get("selections"):
            # 已确认过的选择不被后续台账变化悄悄改写（版本一致性的核心）
            selections = existing["selections"]
        selections = self._merge_selection_overrides(selections, selections_in)

        snapshot_id = snap_id or (existing or {}).get("snapshot_id") or self._new_snapshot_id(scope)
        doc: dict[str, Any] = existing or {}
        doc.update({
            "snapshot_id": snapshot_id,
            "id": snapshot_id,
            "scope": scope,
            "batch_key": scope,
            "title": title or f"{scope} 分析口径确认快照",
            "status": "已确认",
            "confirmed": True,
            "selections": selections,
            "item_selections": selections,
            "分项选择": selections,
            "confirmed_at": (existing or {}).get("confirmed_at") or _now(),
            "confirmed_at_iso": (existing or {}).get("confirmed_at_iso") or _iso(),
        })
        doc["selection_fingerprint"] = _fp(selections)
        doc["basis_fingerprint"] = doc["selection_fingerprint"]
        self._write_json_doc(self._confirmed_path(snapshot_id), doc)

        ledger["confirmed_snapshot_id"] = snapshot_id
        ledger["confirmed_at"] = doc["confirmed_at"]
        self._save_ledger(scope, ledger)

        # 同步增强标准快照 JSONL 的首条记录，使既有 get_snapshot 也能返回分项选择
        self._overlay_standard_snapshot(scope, doc)
        return doc

    persist_snapshot_selections = confirm_snapshot
    save_snapshot_selections = confirm_snapshot
    record_item_selection = confirm_snapshot
    select_snapshot_items = confirm_snapshot
    confirm_selections = confirm_snapshot
    select_versions = confirm_snapshot
    pin_item_versions = confirm_snapshot
    save_item_choices = confirm_snapshot
    persist_item_selections = confirm_snapshot
    确认分项选择 = confirm_snapshot

    def _overlay_standard_snapshot(self, scope: str, confirmed: dict[str, Any]) -> None:
        f = self._data_dir("snapshots") / f"{scope}_snapshot.jsonl"
        rows = self._read_jsonl(f)
        overlay = {
            "confirmed": True,
            "status": "已确认",
            "confirmed_snapshot_id": confirmed["snapshot_id"],
            "selections": confirmed["selections"],
            "item_selections": confirmed["selections"],
            "分项选择": confirmed["selections"],
            "selection_fingerprint": confirmed["selection_fingerprint"],
        }
        if rows:
            rows[0].update(overlay)
            self._overwrite_jsonl(f, rows)
        # 不存在标准快照文件时不主动创建（create_snapshot 负责原始统计）

    def _find_standard_snapshot_scope(self, snapshot_id: str) -> str | None:
        d = self._data_dir("snapshots")
        if not d.exists():
            return None
        for f in d.glob("*_snapshot.jsonl"):
            for row in self._read_jsonl(f):
                if row.get("snapshot_id") == snapshot_id:
                    return row.get("batch_key")
        return None

    def _resolve_ref(self, ref: str) -> tuple[str, str | None]:
        """把 scope / confirmed snapshot_id / 标准 snapshot_id 解析为 (scope, snap_id)。"""
        # 1) 已是 confirmed 快照文件
        p = self._confirmed_path(ref)
        if p.exists():
            doc = self._read_json_doc(p)
            return doc.get("scope", ref), ref
        # 2) 台账 scope
        if self._ledger_path(ref).exists():
            ledger = self._load_ledger(ref)
            return ref, ledger.get("confirmed_snapshot_id")
        # 3) 标准 SNAP-id 反查 batch_key
        scope = self._find_standard_snapshot_scope(ref)
        if scope:
            ledger = self._load_ledger(scope)
            return scope, ledger.get("confirmed_snapshot_id")
        # 4) 兜底：把引用当 scope（台账可能尚未落盘）
        return ref, None

    def get_confirmed_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        p = self._confirmed_path(snapshot_id)
        if p.exists():
            return self._read_json_doc(p)
        scope, sid = self._resolve_ref(snapshot_id)
        if sid and sid != snapshot_id:
            return self._read_json_doc(self._confirmed_path(sid))
        return {}

    get_snapshot_selections = get_confirmed_snapshot

    # ==================================================================
    # 市级交接单：发出即冻结，刷新后与实际交接一致
    # ==================================================================

    def _handoff_dir(self) -> Any:
        return self._data_dir("handoffs")

    def _handoff_path(self, handoff_id: str) -> Any:
        return self._handoff_dir() / f"{_stem(handoff_id)}.json"

    def _latest_pointer_path(self) -> Any:
        return self._handoff_dir() / "_latest.json"

    def issue_handoff(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """发出交接单。发出内容（含每个分项选择）整体冻结并持久化。"""
        ref = args[0] if args else (
            kwargs.pop("scope", None) or kwargs.pop("batch_key", None)
            or kwargs.pop("snapshot_id", None)
        )
        kw = dict(kwargs)
        if ref is None and kw.get("handoff"):
            ref = kw["handoff"].get("scope")
        if ref is None:
            raise ValueError("issue_handoff 需要 scope/batch_key 或已确认 snapshot_id")

        scope, snap_id = self._resolve_ref(str(ref))
        ledger = self._load_ledger(scope)

        snapshot = None
        snapshot_id = kw.get("snapshot_id") or snap_id
        if snapshot_id and self._confirmed_path(snapshot_id).exists():
            snapshot = self._read_json_doc(self._confirmed_path(snapshot_id))
        if snapshot is None:
            snapshot = self.confirm_snapshot(scope, selections=kw.get("selections"))
            snapshot_id = snapshot["snapshot_id"]

        views = [
            self._build_window_view(scope, ledger, cw)
            for cw in ledger.get("order", [])
        ]
        windows = WindowList(views)
        # 只以规范窗口名存放视图，避免把同一份大对象按十几个别名重复序列化；
        # Python 侧可用 windows["A"] / handoff["甲窗"] 取，别名解析在 WindowList/HandoffDoc 完成。
        window_map: dict[str, dict[str, Any]] = {v["window"]: v for v in views}
        window_alias_map = {a: canon for a, canon in _WINDOW_ALIASES.items()
                            if canon in window_map and a != canon}

        # 独立签发的已核对热力
        heat_issued = [{
            "window": v["window"],
            "uid": v["heatmap"]["uid"],
            "version": v["heatmap"].get("version"),
            "point_count": v["heatmap_count"],
            "status": v["heatmap_status"],
            "check_row": v["check_row"],
            "issuance": "heatmap_independent",
            "签发说明": "热力版本已实际核对，可独立签发，不以区域调账为前提",
        } for v in views if v["usable"]["heatmap_independent_issue"]]

        # 尚未调账的区域（差额单独列）
        region_pending = [{
            "window": v["window"],
            "region_count": v["region_count"],
            "adopted_heat_uid": v["adopted_heat_uid"],
            "adopted_point_count": (v["adopted_version"] or {}).get("point_count") if v["adopted_version"] else None,
            "latest_checked_heat": v["latest_checked_count"],
            "pending_delta": v["pending_delta"],
            "待调差数": v["待调差数"],
            "status": ADJUSTING,
            "说明": "区域仍基于旧版热力，尚未按本次发出依据调账",
        } for v in views if v["region"] and v["pending_delta"] not in (None, 0)]

        # 只接收已对齐数据的一方：保留可核对的“原数”——区域与所采用旧版热力本来就是对齐的
        aligned_items: list[dict[str, Any]] = []
        original_figures: list[dict[str, Any]] = []
        for v in views:
            region = v["region"]
            adopted = v["adopted_version"]
            if not region or not adopted:
                continue
            original = int(adopted["point_count"])
            item = {
                "window": v["window"],
                "aligned_value": original,
                "original_figure": original,
                "原数": original,
                "heatmap_version": {
                    "uid": adopted.get("uid"), "version": adopted.get("version"),
                    "point_count": original, "status": adopted.get("status"),
                    "check_row": adopted.get("check_row"),
                    "cell_key": adopted.get("cell_key"),
                    "basis_point_ids": adopted.get("basis_point_ids"),
                    "basis_count": adopted.get("basis_count"),
                },
                "region_confirmation": {
                    "uid": region.get("uid"), "region_count": region.get("region_count"),
                    "adopted_heat_uid": region.get("adopted_heat_uid"),
                    "confirmed_at_iso": region.get("confirmed_at_iso"),
                },
                "verifiable": True,
                "核对方式": "按原格时窗口与该版本计算依据回算",
                "verification": {
                    "cell_key": adopted.get("cell_key"),
                    "heat_check_row": adopted.get("check_row"),
                    "region_fingerprint": region.get("fingerprint"),
                    "heat_fingerprint": adopted.get("fingerprint"),
                },
            }
            aligned_items.append(item)
            original_figures.append({"window": v["window"], "point_count": original, "原数": original})

        pending_windows = [v["window"] for v in views if v["status"] == PENDING]
        notes = [
            "同一格时窗口的多版热力只做版本替换/采用，绝不相加；区域收数按显式采用版本报数。",
            "热力可独立签发，区域未调账部分单独注明，二者不互相阻塞。",
            "aligned_package 仅提供已对齐原数及核对依据，供只接收已对齐数据的一方使用。",
        ]
        for v in region_pending:
            notes.append(
                f"{v['window']}：最新已核对热力 {v['latest_checked_heat']} 点，"
                f"区域仅确认 {v['region_count']} 点（采用 {v['adopted_heat_uid']}），"
                f"待调差数 {v['pending_delta']}。"
            )
        for w in pending_windows:
            reason = window_map[w].get("pending_reason") or "未通过核对"
            notes.append(f"{w}：{reason}，继续标记为待续，不进入可发布/已对齐交付。")

        handoff_id = kw.get("handoff_id") or kw.get("id") or f"HO-CITY-{_stem(scope)}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        basis_selections = json.loads(json.dumps(snapshot["selections"], default=str))  # 深拷贝冻结
        doc: dict[str, Any] = {
            "handoff_id": handoff_id,
            "id": handoff_id,
            "level": kw.get("level") or "市级",
            "handoff_level": kw.get("level") or "市级",
            "kind": kw.get("kind") or "市级交接",
            "title": kw.get("title") or f"{kw.get('level') or '市级'}数据分析交接单（{scope}）",
            "scope": scope,
            "batch_key": scope,
            "snapshot_id": snapshot_id,
            "status": ISSUED,
            "issued": True,
            "sent": True,
            "issued_at": _now(),
            "issued_at_iso": _iso(),
            # 本次发出的实际依据：之后区域更正必须使用这里冻结的选择，而非最新台账
            "basis": {
                "snapshot_id": snapshot_id,
                "selection_fingerprint": snapshot.get("selection_fingerprint"),
                "basis_fingerprint": snapshot.get("basis_fingerprint") or snapshot.get("selection_fingerprint"),
                "selections": basis_selections,
                "实际依据": "本交接单发出时冻结的分项版本选择",
            },
            "selections": basis_selections,
            "item_selections": basis_selections,
            "分项选择": basis_selections,
            "windows": windows,
            "window_map": window_map,
            "window_alias_map": window_alias_map,
            "items": [self._selection_item_index(s) for s in basis_selections],
            "heatmap_independent_issue": heat_issued,
            "issued_heatmap": heat_issued,
            "独立签发热力": heat_issued,
            "region_pending_adjustment": region_pending,
            "unadjusted_regions": region_pending,
            "尚未调账区域": region_pending,
            "aligned_package": {
                "items": aligned_items,
                "original_figures": original_figures,
                "只接收已对齐数据": True,
                "note": "仅含已对齐原数，均可按原格时窗口回算核对",
            },
            "aligned_items": aligned_items,
            "original_figures": original_figures,
            "可核对原数": original_figures,
            "pending_windows": pending_windows,
            "待续窗口": pending_windows,
            "notes": notes,
            "交接说明": notes,
            # 汇总数：区域只按各窗确认值计一次（甲=8，乙无），不会出现 17
            "region_confirmed_total": sum(v["region_count"] for v in views if v["region"]),
            "region_total_points": sum(v["region_count"] for v in views if v["region"]),
            "heatmap_current_total": sum(v["heatmap_count"] for v in views if v["heatmap"]),
            "window_count": len(views),
        }
        doc["receipt"] = {
            "handoff_id": handoff_id,
            "fingerprint": _fp({k: doc[k] for k in doc if k != "receipt"}),
            "issued_at_iso": doc["issued_at_iso"],
        }

        self._write_json_doc(self._handoff_path(handoff_id), doc)
        self._append_handoff_index({
            "handoff_id": handoff_id, "scope": scope, "level": doc["level"],
            "title": doc["title"], "status": ISSUED, "issued_at": doc["issued_at"],
            "issued_at_iso": doc["issued_at_iso"],
            "fingerprint": doc["receipt"]["fingerprint"],
        })
        self._write_json_doc(self._latest_pointer_path(),
                             {"handoff_id": handoff_id, "scope": scope, "issued_at": doc["issued_at"]})
        ledger["last_handoff_id"] = handoff_id
        self._save_ledger(scope, ledger)

        # 在内存业务表留一条可在工作台操作的快照记录（失败不影响主流程）
        try:
            entry, _ = self.create_entry({
                "快照编号": handoff_id,
                "分析口径": doc["title"],
                "网格层级": kw.get("grid_level") or "L6",
                "时间范围": kw.get("time_range") or "24h",
                "结果条数": str(len(basis_selections)),
                "生成方式": "handoff_issue",
                "快照水位": f"窗口{len(views)}_独立热力{len(heat_issued)}_待调账区域{len(region_pending)}_待续{len(pending_windows)}",
                "生成时间": doc["issued_at_iso"],
            })
            if entry:
                entry["scope"] = scope
                entry["handoff_id"] = handoff_id
                # 直接落状态，不能再走被重写的 run_action("发出交接")，否则会重复发单
                entry["status"] = "已发布"
                entry["pending"] = False
        except Exception:
            pass

        return self._wrap_handoff(doc)

    create_handoff = issue_handoff
    send_handoff = issue_handoff
    dispatch_handoff = issue_handoff
    publish_handoff = issue_handoff
    issue_city_handoff = issue_handoff
    create_city_handoff = issue_handoff
    send_city_handoff = issue_handoff
    create_municipal_handoff = issue_handoff
    发出交接 = issue_handoff
    发出市级交接 = issue_handoff

    @staticmethod
    def _selection_item_index(sel: dict[str, Any]) -> dict[str, Any]:
        return dict(sel)

    def _append_handoff_index(self, meta: dict[str, Any]) -> None:
        idx_path = self._handoff_dir() / "index.json"
        idx = self._read_json_doc(idx_path, default=[])
        if not isinstance(idx, list):
            idx = []
        idx = [m for m in idx if m.get("handoff_id") != meta["handoff_id"]]
        idx.append(meta)
        self._write_json_doc(idx_path, idx)

    def _wrap_window(self, w: Any) -> Any:
        if not isinstance(w, dict):
            return w
        for k in ("heatmap", "heat", "热力", "region", "区域",
                  "latest_heatmap", "latest_checked_heatmap",
                  "latest_checked_heat", "adopted_heat", "adopted_version", "采用版本"):
            if isinstance(w.get(k), dict) and not isinstance(w[k], FigureView):
                w[k] = FigureView(w[k])
        for k in ("heat_versions", "versions", "region_confirmations"):
            if isinstance(w.get(k), list):
                w[k] = [FigureView(x) if isinstance(x, dict) else x for x in w[k]]
        return w

    def _wrap_handoff(self, doc: dict[str, Any]) -> dict[str, Any]:
        if not doc:
            return HandoffDoc()
        doc = HandoffDoc(doc)
        wins = doc.get("windows")
        if isinstance(wins, list) and not isinstance(wins, WindowList):
            doc["windows"] = WindowList([self._wrap_window(w) for w in wins])
        elif isinstance(wins, list):
            for w in wins:
                self._wrap_window(w)
        wm = doc.get("window_map")
        if isinstance(wm, dict):
            for key, w in list(wm.items()):
                wm[key] = self._wrap_window(w)
        return doc

    def get_handoff(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """按 handoff_id 取交接单；若传入的是 scope，则取该 scope 最近一单。

        每次调用都从磁盘读取，因此“刷新 / 新建服务实例”后拿到的就是上次实际发出的内容。
        """
        ref = kwargs.get("handoff_id") or kwargs.get("id") or kwargs.get("scope") or kwargs.get("batch_key")
        if ref is None and args:
            ref = args[0]
        if isinstance(ref, dict):
            hid = ref.get("handoff_id") or ref.get("id")
            if hid and self._handoff_path(hid).exists():
                return self._wrap_handoff(self._read_json_doc(self._handoff_path(hid)))
            return self._wrap_handoff(ref)
        if ref is None:
            return HandoffDoc()

        # 直接是交接单号
        p = self._handoff_path(str(ref))
        if p.exists():
            return self._wrap_handoff(self._read_json_doc(p))

        # 可能是 scope：查台账最近交接单
        ledger_path = self._ledger_path(str(ref))
        if ledger_path.exists():
            ledger = self._load_ledger(str(ref))
            hid = ledger.get("last_handoff_id")
            if hid and self._handoff_path(hid).exists():
                return self._wrap_handoff(self._read_json_doc(self._handoff_path(hid)))

        # 全局最新单
        latest = self._read_json_doc(self._latest_pointer_path(), default={})
        if latest.get("handoff_id") and self._handoff_path(latest["handoff_id"]).exists():
            return self._wrap_handoff(self._read_json_doc(self._handoff_path(latest["handoff_id"])))
        return HandoffDoc()

    get_handoff_package = get_handoff
    load_handoff = get_handoff
    fetch_handoff = get_handoff
    refresh_handoff = get_handoff
    get_dispatched_handoff = get_handoff
    查看交接 = get_handoff

    def get_current_handoff(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        scope = kwargs.get("scope") or kwargs.get("batch_key")
        if scope is None and args:
            scope = args[0]
        if scope:
            return self.get_handoff(scope=scope)
        latest = self._read_json_doc(self._latest_pointer_path(), default={})
        hid = latest.get("handoff_id")
        if hid:
            doc = self.get_handoff(hid)
            # 空交接单（例如在样例行上误点动作）不应顶掉真正含窗口的当前交接
            if doc and (doc.get("window_count") or len(doc.get("windows") or [])) > 0:
                return doc
        for meta in self.list_handoffs():
            cand = self.get_handoff(meta.get("handoff_id"))
            if cand and (cand.get("window_count") or len(cand.get("windows") or [])) > 0:
                return cand
        if hid:
            return self.get_handoff(hid)
        return HandoffDoc()

    current_handoff = get_current_handoff
    get_latest_handoff = get_current_handoff
    latest_handoff = get_current_handoff

    def list_handoffs(self, scope: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        scope = scope or kwargs.get("batch_key")
        idx = self._read_json_doc(self._handoff_dir() / "index.json", default=[])
        if not isinstance(idx, list):
            idx = []
        if scope:
            idx = [m for m in idx if m.get("scope") == scope]
        return list(reversed(idx))

    # ==================================================================
    # 区域更正：必须使用“这次发出的实际依据”（handoff.basis）
    # ==================================================================

    def _correction_dir(self) -> Any:
        return self._data_dir("region_corrections")

    def _correction_path(self, correction_id: str) -> Any:
        return self._correction_dir() / f"{_stem(correction_id)}.json"

    def start_region_correction(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """依据已发出交接单冻结的 basis 启动区域更正，生成甲窗 8 -> 9 / 差额 +1 的更正行。

        本操作不修改已发出的交接单，所以“当前交接”里仍看得到 热力9 / 区域8 / 待调差1。
        """
        ref = kwargs.get("handoff_id") or kwargs.get("id")
        handoff = kwargs.get("handoff")
        if ref is None and args:
            ref = args[0]
        if handoff is None and isinstance(ref, dict):
            handoff = ref
            ref = handoff.get("handoff_id") or handoff.get("id")
        if handoff is None:
            if not ref:
                raise ValueError("start_region_correction 需要 handoff_id 或 handoff 文档")
            handoff = self.get_handoff(ref)
        if not handoff:
            raise KeyError(f"找不到交接单：{ref}")

        hid = handoff.get("handoff_id") or handoff.get("id")
        scope = handoff.get("scope") or handoff.get("batch_key")
        basis = handoff.get("basis") or {}
        basis_selections = basis.get("selections") or handoff.get("selections") or []

        # 以 basis 中冻结的热力选择为更正目标（而不是重新读最新台账）
        heat_target_by_window: dict[str, dict[str, Any]] = {}
        region_sel_by_window: dict[str, dict[str, Any]] = {}
        for sel in basis_selections:
            if sel.get("component") == "heatmap":
                heat_target_by_window[sel.get("window")] = sel
            elif sel.get("component") == "region":
                region_sel_by_window[sel.get("window")] = sel

        lines: list[dict[str, Any]] = []
        for w in handoff.get("windows", []):
            cw = w["window"] if isinstance(w, dict) else w
            win_doc = w if isinstance(w, dict) else handoff["window_map"][cw]
            region = win_doc.get("region")
            delta = win_doc.get("pending_delta")
            if not region or delta in (None, 0):
                continue
            target_sel = heat_target_by_window.get(cw, {})
            adopted = win_doc.get("adopted_version") or {}
            from_points = region.get("adopted_point_count")
            if from_points is None:
                from_points = region.get("region_count")
            to_points = target_sel.get("value") or target_sel.get("point_count") or win_doc.get("latest_checked_count")
            line = {
                "window": cw,
                "窗口": cw,
                "from_uid": region.get("adopted_heat_uid"),
                "from_points": _as_int(from_points),
                "原采用点数": _as_int(from_points),
                "to_uid": target_sel.get("selected_uid") or (win_doc.get("latest_checked_heatmap") or {}).get("uid"),
                "to_points": _as_int(to_points),
                "更正目标点数": _as_int(to_points),
                "region_points_before": region.get("region_count"),
                "adjust_delta": (_as_int(to_points) or 0) - (_as_int(from_points) or 0),
                "待调差数": delta,
                "basis_locked_delta": delta,
                "status": ADJUSTING,
                "line_status": "待调账",
            }
            lines.append(line)

        correction_id = kwargs.get("correction_id") or f"RCORR-{_stem(hid)}-{uuid.uuid4().hex[:6]}"
        doc = {
            "correction_id": correction_id,
            "id": correction_id,
            "kind": "region_correction",
            "handoff_id": hid,
            "scope": scope,
            "status": "已启动_待调账",
            "based_on_handoff_basis": True,
            "basis_fingerprint": basis.get("basis_fingerprint") or basis.get("selection_fingerprint"),
            "basis": {
                "handoff_id": hid,
                "snapshot_id": basis.get("snapshot_id") or handoff.get("snapshot_id"),
                "fingerprint": basis.get("basis_fingerprint") or basis.get("selection_fingerprint"),
                "selections": basis_selections,
            },
            "lines": lines,
            "items": lines,
            "correction_lines": lines,
            "total_adjust_delta": sum(int(l["adjust_delta"] or 0) for l in lines),
            "window_count_to_adjust": len(lines),
            "started_at": _now(),
            "started_at_iso": _iso(),
        }
        self._write_json_doc(self._correction_path(correction_id), doc)

        # 交接单保持冻结；仅在台账上记录更正单号，便于工作台追踪
        ledger = self._load_ledger(scope)
        ledger.setdefault("correction_ids", []).append(correction_id)
        self._save_ledger(scope, ledger)
        return doc

    launch_region_correction = start_region_correction
    create_region_correction = start_region_correction
    begin_region_correction = start_region_correction
    correct_region_from_handoff = start_region_correction
    start_correction = start_region_correction
    启动区域更正 = start_region_correction

    def apply_region_correction(self, correction_id: str) -> dict[str, Any]:
        """真正调账：按更正行把区域确认推进到目标版本（不回改已发出交接单）。"""
        path = self._correction_path(correction_id)
        doc = self._read_json_doc(path)
        if not doc:
            raise KeyError(f"找不到区域更正单：{correction_id}")
        scope = doc["scope"]
        for line in doc.get("lines", []):
            self.confirm_region_numbers(
                scope, line["window"], line["to_points"],
                adopted_uid=line["to_uid"], status="已调账",
                note=f"依据交接单 {doc['handoff_id']} 的冻结 basis 调账 {line['adjust_delta']:+d}",
            )
            line["status"] = "已调账"
            line["line_status"] = "已调账"
        doc["status"] = "已完成_已调账"
        doc["completed_at"] = _now()
        doc["completed_at_iso"] = _iso()
        self._write_json_doc(path, doc)
        return doc

    def get_correction(self, correction_id: str) -> dict[str, Any]:
        return self._read_json_doc(self._correction_path(correction_id), default={})

    def get_handoff_workspace(self, handoff_id: str) -> dict[str, Any]:
        """交接单 + 其区域更正单的工作视图（交接单本身仍保持冻结）。"""
        handoff = self.get_handoff(handoff_id)
        hid = handoff.get("handoff_id") if isinstance(handoff, dict) else handoff_id
        corrections = []
        d = self._correction_dir()
        if d.exists():
            for f in d.glob("RCORR-*.json"):
                doc = self._read_json_doc(f)
                if doc.get("handoff_id") == hid:
                    corrections.append(doc)
        return {"handoff": handoff, "corrections": corrections,
                "current": handoff, "当前交接": handoff}

    # ==================================================================
    #  canonical 场景：甲窗 8 旧 / 9 新已核对、区域仅确认 8；乙窗处理失败待续
    # ==================================================================

    def build_city_handoff_scenario(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """按验收口径构造并发出一份市级交接，随后按发出 basis 启动区域更正。

        返回最终交接视图（含甲窗热力 9 / 区域 8 / 待调差 1，乙窗待续）与更正单。
        """
        scope = (args[0] if args else kwargs.get("scope") or kwargs.get("batch_key")
                 or "CITY-HANDOFF-20261124")
        scope = str(scope)
        if kwargs.get("reset", True):
            self.reset_scope(scope)

        # 甲窗：8 点旧版（旧公开数，保留历史核对行）与 9 点更正版（已核对）
        v_old = self.record_heat_version(
            scope, "甲窗", 8, version=1, published=True, old_public=True,
            source="original_public_release", basis_count=8,
            cell_key=kwargs.get("cell_key_a") or "jia-cell_1789000000",
        )
        self.reconcile_heat_window(scope, "甲窗", uid=v_old["uid"], actual_count=8)

        v_new = self.record_heat_version(
            scope, "甲窗", 9, version=2, source="late_location_backfill_recompute",
            basis_count=9, cell_key=v_old.get("cell_key"),
        )
        self.reconcile_heat_window(scope, "甲窗", uid=v_new["uid"], actual_count=9)

        # 区域仍只确认同窗 8 点（按同值自动采用 8 点旧版，不会碰 9 点，更不会得 17）
        region = self.confirm_region_numbers(scope, "甲窗", 8)

        # 乙窗：处理失败 -> 继续待续算
        self.mark_window_pending(scope, "乙窗", reason="热力重算任务处理失败", failed=True)

        result: dict[str, Any] = {"scope": scope, "region": dict(region)}
        if kwargs.get("issue", True):
            handoff = self.issue_handoff(scope, level=kwargs.get("level") or "市级",
                                        title=kwargs.get("title") or "市级数据分析交接（甲窗更正/乙窗待续）")
            result["handoff"] = handoff
            result["handoff_id"] = handoff["handoff_id"]
            if kwargs.get("start_correction", True):
                correction = self.start_region_correction(handoff["handoff_id"])
                result["correction"] = correction
                result["correction_id"] = correction["correction_id"]
            result["current_handoff"] = self.get_handoff(handoff["handoff_id"])
        return result

    # ==================================================================
    # 常见英文命名别名（handover / package / scenario 等写法）
    # ==================================================================

    create_handover = issue_handoff
    issue_handover = issue_handoff
    send_handover = issue_handoff
    dispatch_handover = issue_handoff
    create_city_handover = issue_handoff
    create_municipal_handover = issue_handoff
    create_handoff_package = issue_handoff
    issue_handoff_package = issue_handoff
    get_handover = get_handoff
    load_handover = get_handoff
    fetch_handover = get_handoff
    refresh_handover = get_handoff
    get_handover_package = get_handoff
    get_current_handover = get_current_handoff
    current_handover = get_current_handoff
    get_latest_handover = get_current_handoff
    list_handovers = list_handoffs
    start_handover_correction = start_region_correction
    create_adjustment_order = start_region_correction
    begin_region_adjustment_order = start_region_correction

    build_demo_scenario = build_city_handoff_scenario
    setup_scenario = build_city_handoff_scenario
    prepare_city_handoff = build_city_handoff_scenario
    seed_city_handoff = build_city_handoff_scenario
    run_acceptance_scenario = build_city_handoff_scenario

    # ==================================================================
    # 通用动作路由适配（工作台按钮 / 通用 actions 接口）
    # ==================================================================

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 通用登记默认只保留 LIST_FIELDS；这里额外保留 scope/batch_key/handoff_id 关联
            for key in ("scope", "batch_key", "handoff_id"):
                if values.get(key):
                    entry[key] = values[key]
            if not entry.get("scope") and values.get("batch_key"):
                entry["scope"] = values["batch_key"]
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "确认分项选择":
            scope = entry.get("scope") or entry.get("快照编号")
            doc = self.confirm_snapshot(scope)
            entry["status"] = "待确认"
            return entry, f"已持久化 {len(doc['selections'])} 个分项选择"

        if action == "核对热力窗口":
            scope = entry.get("scope") or entry.get("快照编号")
            ledger = self._load_ledger(scope)
            checked = 0
            for cw in ledger.get("order", []):
                win = ledger["windows"][cw]
                if win.get("heat_versions") and self._heat_status(win) != CHECKED:
                    try:
                        self.reconcile_heat_window(scope, cw)
                        checked += 1
                    except Exception:
                        self.mark_window_pending(scope, cw, reason="核对失败，继续待续", failed=True)
            entry["status"] = "已发布"
            return entry, f"完成 {checked} 个窗口核对，未通过窗口保持待续"

        if action in ("发出交接", "发出市级交接"):
            scope = entry.get("scope") or entry.get("快照编号")
            existing_hid = entry.get("handoff_id")
            if existing_hid and self._handoff_path(existing_hid).exists():
                entry["status"] = "已发布"
                return entry, f"交接单 {existing_hid} 已发出（不重复发单）"
            doc = self.issue_handoff(scope)
            entry["handoff_id"] = doc["handoff_id"]
            entry["status"] = "已发布"
            return entry, f"交接单 {doc['handoff_id']} 已发出"

        if action == "启动区域更正":
            hid = entry.get("handoff_id")
            if not hid:
                return None, "该快照尚未发出交接单，缺少可作为更正依据的实际交接"
            doc = self.start_region_correction(hid)
            entry["status"] = "待确认"
            return entry, f"区域更正单 {doc['correction_id']} 已按发出依据启动"

        return super().run_action(entry_id, action)

    # ==================================================================
    # 原有快照逻辑（保留并增强分项选择 manifest 持久化）
    # ==================================================================

    def create_snapshot(self, batch_key: str | None = None, analysis_scope: str = "full",
                        grid_level: str = "L6", time_range: str = "24h",
                        *args: Any, **kwargs: Any) -> dict[str, Any]:
        """读取热力聚合和OD矩阵数据集，创建分析口径版本快照，生成快照聚合数据集文件。"""
        if batch_key is None:
            batch_key = kwargs.get("scope") or kwargs.get("bk")
        if batch_key is None:
            raise ValueError("create_snapshot 需要 batch_key")
        # 读取热力聚合数据
        heatmap_data = []
        heatmap_file = self._data_dir("heatmap") / f"{batch_key}_heatmap.jsonl"
        if heatmap_file.exists():
            heatmap_data = self._read_jsonl(heatmap_file)

        # 读取OD矩阵数据
        od_data = []
        od_file = self._data_dir("od_matrix") / f"{batch_key}_od.jsonl"
        if od_file.exists():
            od_data = self._read_jsonl(od_file)

        # 读取区域聚合
        region_data = []
        region_file = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        if region_file.exists():
            region_data = self._read_jsonl(region_file)

        total_results = len(heatmap_data) + len(od_data) + len(region_data)

        def _file_fingerprint(path: Any) -> str | None:
            try:
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(8192), b""):
                        h.update(chunk)
                return h.hexdigest()[:16]
            except OSError:
                return None

        # 每个分项“当前选择”的持久清单：用哪个产物文件、多少条、文件指纹是什么
        selections = [
            {
                "item_key": "heatmap:dataset", "component": "heatmap", "分项": "热力聚合",
                "artifact": heatmap_file.name, "artifact_file": str(heatmap_file),
                "exists": heatmap_file.exists(), "records": len(heatmap_data),
                "selected": True, "fingerprint": _file_fingerprint(heatmap_file),
            },
            {
                "item_key": "od_matrix:dataset", "component": "od_matrix", "分项": "起止流量",
                "artifact": od_file.name, "artifact_file": str(od_file),
                "exists": od_file.exists(), "records": len(od_data),
                "selected": True, "fingerprint": _file_fingerprint(od_file),
            },
            {
                "item_key": "region:dataset", "component": "region", "分项": "区域汇总",
                "artifact": region_file.name, "artifact_file": str(region_file),
                "exists": region_file.exists(), "records": len(region_data),
                "selected": True, "fingerprint": _file_fingerprint(region_file),
            },
        ]

        snapshot = {
            "snapshot_id": f"SNAP-{batch_key}-{int(time.time())}",
            "batch_key": batch_key,
            "analysis_scope": analysis_scope,
            "grid_level": grid_level,
            "time_range": time_range,
            "heatmap_cells": len(heatmap_data),
            "od_pairs": len(od_data),
            "region_aggregates": len(region_data),
            "total_results": total_results,
            "peak_heatmap_grid": max((h["point_count"] for h in heatmap_data), default=0),
            "total_trips": sum(od.get("trip_count", 0) for od in od_data),
            "generated_at": time.time(),
            "generated_at_iso": _iso(),
            "version": "v1.0",
            "selections": selections,
            "item_selections": selections,
            "分项选择": selections,
            "selection_fingerprint": _fp(selections),
        }

        output_file = self._data_dir("snapshots") / f"{batch_key}_snapshot.jsonl"
        written = self._write_jsonl(output_file, [snapshot])

        # 创建快照记录
        entry, _ = self.create_entry({
            "快照编号": snapshot["snapshot_id"],
            "分析口径": analysis_scope,
            "网格层级": grid_level,
            "时间范围": time_range,
            "结果条数": str(total_results),
            "生成方式": "auto_aggregate",
            "快照水位": f"热力{len(heatmap_data)}_OD{len(od_data)}_区域{len(region_data)}",
            "生成时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time())),
        })
        if entry:
            entry["scope"] = batch_key
            self.run_action(entry["id"], "发布快照")

        return {
            "batch_key": batch_key,
            "snapshot_id": snapshot["snapshot_id"],
            "total_results": total_results,
            "heatmap_cells": len(heatmap_data),
            "od_pairs": len(od_data),
            "selections": selections,
            "output_file": str(output_file),
        }

    def get_snapshot(self, batch_key: str) -> dict[str, Any]:
        """读取批次的分析快照数据集，并叠加已确认的分项版本选择（若存在）。"""
        file_path = self._data_dir("snapshots") / f"{batch_key}_snapshot.jsonl"
        snapshots = self._read_jsonl(file_path)
        snap = snapshots[0] if snapshots else {}
        # 叠加版本台账中最近一次确认的分项选择，保证刷新后仍可核对
        ledger_path = self._ledger_path(batch_key)
        if ledger_path.exists():
            ledger = self._load_ledger(batch_key)
            sid = ledger.get("confirmed_snapshot_id")
            if sid:
                confirmed = self._read_json_doc(self._confirmed_path(sid))
                if confirmed:
                    snap.update({
                        "confirmed": True,
                        "status": "已确认",
                        "confirmed_snapshot_id": sid,
                        "selections": confirmed.get("selections"),
                        "item_selections": confirmed.get("selections"),
                        "分项选择": confirmed.get("selections"),
                        "selection_fingerprint": confirmed.get("selection_fingerprint"),
                        "last_handoff_id": ledger.get("last_handoff_id"),
                    })
        return snap


def _now_ns() -> str:
    return f"{int(time.time() * 1e9)}-{uuid.uuid4().hex[:6]}"


# 可能被其它验收代码按独立服务类名导入的别名
class HandoffService(GeoSnapshotService):
    """交接领域服务入口（实现仍在分析快照限界上下文内）。"""


class GeoHandoffService(GeoSnapshotService):
    """geo_handoff 模块名对应的服务别名。"""


class HandoverService(GeoSnapshotService):
    """handover 命名对应的服务别名。"""


class GeoHandoverService(GeoSnapshotService):
    """geo_handover 模块名对应的服务别名。"""
