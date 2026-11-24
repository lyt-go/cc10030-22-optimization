# 地理空间轨迹与网格聚合分析平台

面向定位点采集、坐标标准化、网格切分、轨迹分段驻留识别、空间关联聚合与区域维度产出的地理空间数据分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 定位源接入 | `geo_source` | 采集批次 | 批次编号、采集终端、定位方式 |
| 坐标标准化 | `coord_transform` | 转换规则 | 规则编号、源坐标系、目标坐标系 |
| 轨迹切分 | `track_segment` | 轨迹分段 | 分段编号、终端编号、起始时间 |
| 驻留识别 | `dwell_detect` | 驻留事件 | 事件编号、分段编号、中心坐标 |
| 网格索引 | `grid_index` | 网格单元 | 网格编号、网格层级、边界范围 |
| 围栏判定 | `geofence_rule` | 电子围栏 | 围栏编号、围栏形状、顶点序列 |
| 空间关联 | `spatial_join` | 关联任务 | 任务编号、左数据集、右数据集 |
| 兴趣点匹配 | `poi_match` | 匹配候选 | 候选编号、驻留事件、兴趣点编号 |
| 区域维度 | `region_dim` | 区域层级 | 区域编号、上级区域、区域名称 |
| 热力聚合 | `heatmap_aggregate` | 聚合窗口 | 窗口编号、网格层级、时间粒度 |
| 起止流量 | `od_matrix` | 流量单元 | 单元编号、起点网格、终点网格 |
| 路径还原 | `route_restore` | 还原路段 | 路段编号、分段编号、路段序列 |
| 密度估算 | `density_estimate` | 密度网格 | 估算编号、网格编号、带宽参数 |
| 轨迹质量 | `geo_quality` | 质量报告 | 报告编号、批次编号、漂移点数 |
| 点位归并 | `dedup_merge` | 归并批次 | 归并编号、批次编号、去重键列 |
| 分析快照 | `geo_snapshot` | 版本快照 | 快照编号、分析口径、网格层级 |
| 历史回补 | `geo_backfill` | 回补任务 | 回补编号、目标数据集、起始日期 |
| 结果导出 | `geo_export` | 导出任务 | 导出编号、快照编号、导出格式 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 分析快照：晚到定位热力更正的版本一致性

晚到定位触发部分格热力重算时，同一“格时窗口”的多版结果（如 8 点旧版 / 9 点更正版）
会在 `GeoSnapshotService` 的版本台账中**全部保留**，任何收数只显式采用一个版本并单列差额，
绝不把两版相加报成 17 点：

- `record_heat_version(scope, window, point_count, ...)`：登记一版热力（只追加版本，不做加法）。
- `reconcile_heat_window(scope, window, uid=..., actual_count=...)`：按原格时窗口与该版本
  自己的计算依据回算，产出“实际核对行”；通过才置「已核对」，否则保持「待续」。
- `confirm_region_numbers(scope, window, count, adopted_uid=...)`：区域收数，显式记录采用哪版。
- `confirm_snapshot(scope)`：持久记录每个分项选择（版本 uid / 数值 / 指纹）。
- `issue_handoff(scope, level="市级")`：发出交接单并冻结 `basis`；单据落盘，刷新/新实例读取一致。
  单内分开给出 `heatmap_independent_issue`（独立签发的热力）、
  `region_pending_adjustment`（未调账区域 + 待调差数）、
  `aligned_package.original_figures`（只接收已对齐数据一方的可核对原数）。
- `start_region_correction(handoff_id)`：严格按交接单发出时冻结的 basis 启动更正
  （甲窗 8→9、差 +1），不改动已发出的交接单。

HTTP 薄层见 `app/routers/handoff.py`（前缀 `/api/geo_snapshot`，含 `handoffs`、`reconcile`、
`region-confirm`、`corrections`、`scenario/city-handoff` 与通用 `invoke/{method_name}`）。
验收场景可一键构造：`POST /api/geo_snapshot/scenario/city-handoff`
（甲窗热力 9 已核对 / 区域仅确认 8 / 待调差 1，乙窗处理失败保持待续）。

仓库已用 `/workspace/scripts/issue_city_handoff.py` 向默认数据目录发出一份固定单号的市级交接：
`HO-CITY-20261124-ISSUED`，更正单 `RCORR-CITY-20261124-BASIS`，紧凑视图见
`/workspace/data/city_handoff_view.json`。

领域：数据工程
子方向：数据分析与数据应用
