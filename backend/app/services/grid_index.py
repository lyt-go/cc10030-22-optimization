"""网格索引领域服务：按空间层级划分网格单元，生成geohash索引并统计落点数量。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GridIndexService(BaseModuleService):
    MODULE = "grid_index"
    ENTITY = "网格单元"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["网格编号", "网格层级", "边界范围", "中心坐标", "落点数量", "索引精度", "生成方式", "索引版本"]
    REQUIRED_FIELDS = ["网格编号", "网格层级", "边界范围"]
    STATUS_ORDER = ["草稿", "待生成", "已生成", "已失效"]
    ACTION_RULES = {"启动生成": "待生成", "确认生成": "已生成", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = []

    def index_points_from_batch(self, batch_key: str, precision: int = 6) -> dict[str, Any]:
        """读取批次转换后的定位点数据文件，按geohash精度对定位点生成网格索引数据集。"""
        points_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not points_file.exists():
            # 尝试直接从采集点读取
            points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据不存在")

        points = self._read_jsonl(points_file)
        if not points:
            return {"batch_key": batch_key, "grids": 0, "total_points": 0}

        grid_map: dict[str, dict[str, Any]] = {}
        for p in points:
            lat = p.get("trans_lat", p.get("lat", 0))
            lon = p.get("trans_lon", p.get("lon", 0))
            gh = self._geohash_encode(lat, lon, precision)
            if gh not in grid_map:
                grid_map[gh] = {
                    "geohash": gh,
                    "precision": precision,
                    "level": f"L{precision}",
                    "point_count": 0,
                    "lats": [],
                    "lons": [],
                }
            grid_map[gh]["point_count"] += 1
            grid_map[gh]["lats"].append(lat)
            grid_map[gh]["lons"].append(lon)

        grids = []
        grid_id = 0
        for gh, info in grid_map.items():
            grid_id += 1
            center_lat = sum(info["lats"]) / len(info["lats"])
            center_lon = sum(info["lons"]) / len(info["lons"])
            grids.append({
                "grid_id": grid_id,
                "geohash": gh,
                "batch_key": batch_key,
                "level": info["level"],
                "precision": precision,
                "center_lat": round(center_lat, 6),
                "center_lon": round(center_lon, 6),
                "point_count": info["point_count"],
                "generated_at": time.time(),
            })

        output_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
        # 网格索引为整份重算数据集，覆盖写入，避免重跑后区域收数重复计数。
        written = self._overwrite_jsonl(output_file, grids)

        for g in grids:
            entry, _ = self.create_entry({
                "网格编号": f"GRID-{batch_key}-{g['grid_id']:03d}",
                "网格层级": g["level"],
                "边界范围": g["geohash"],
                "中心坐标": f"{g['center_lat']},{g['center_lon']}",
                "落点数量": g["point_count"],
                "索引精度": str(precision),
                "生成方式": "geohash",
                "索引版本": "v1.0",
            })
            if entry:
                self.run_action(entry["id"], "确认生成")

        return {
            "batch_key": batch_key,
            "total_points": len(points),
            "grids_generated": written,
            "output_file": str(output_file),
        }

    def get_grid_index(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的网格索引数据集文件。"""
        file_path = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据集不存在")
        grids = self._read_jsonl(file_path)
        return grids, str(file_path)