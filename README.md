# 城市内涝风险可视化平台

> Urban Flood Risk Visualization Platform
> 基于 Vue3 + Cesium + Spring Boot + PostGIS 的城市内涝风险三维可视化与预警系统

---

## 这个项目是什么

把「数据采集 → 空间分析 → 风险建模 → 三维可视化」串成一条完整链路，展示某城市在不同降雨情景下的内涝风险分布，支持时间轴回放与点位查询。

**技术骨架**沿用本科毕业设计《基于 SharpMap 的 PM2.5 信息管理系统》的思路，技术栈整体替换为 2026 年主流方案，场景升级为更有现实价值的城市韧性方向。

---

## 技术栈

| 层 | 技术 | 版本 |
|---|---|---|
| 数据处理 | Python + GeoPandas + Rasterio | 3.13 |
| 空间数据库 | PostgreSQL + PostGIS | 17 + 3.5 |
| 后端 | Spring Boot + MyBatis | 3.x / JDK 21 |
| 前端 | Vue 3 + Vite | 3.x |
| 三维引擎 | Cesium | 1.1xx |

---

## 目录结构

```
urban-flood-gis/
├── README.md                 本文件
├── .gitignore                版本控制忽略规则
├── docs/                     项目文档
│   ├── 方案/                 开发方案、排期
│   ├── 教程/                 环境安装图解
│   ├── 架构说明.md           模块与技术细节
│   ├── 数据说明.md           数据来源与字段定义
│   └── 周记.md               每周进度记录
├── backend/                  Spring Boot 后端（M4 模块）
├── frontend/                 Vue3 + Cesium 前端（M6 模块）
├── pipeline/                 Python 数据处理（M1 / M2 模块）
│   ├── fetch/                数据采集脚本
│   ├── process/              空间处理脚本
│   └── requirements.txt      Python 依赖清单
├── sql/                      数据库脚本
│   ├── 01_schema.sql         建表 + 空间索引
│   └── 02_views.sql          常用视图与查询
├── data/                     数据目录（不进 Git）
│   ├── raw/                  原始数据：爬取的降雨、DEM、OSM
│   ├── clean/                清洗后数据：统一坐标系的 GeoJSON / CSV
│   └── output/               处理产物：风险栅格 GeoTIFF、易涝区矢量
├── scripts/                  一键脚本
│   ├── start-backend.bat     启动后端
│   ├── start-frontend.bat    启动前端
│   └── init-db.bat           初始化数据库
├── assets/                   截图、架构图、演示视频封面
└── notes/                    学习笔记、踩坑记录
```

---

## 快速开始

### 1. 环境准备

见 `docs/教程/PostgreSQL安装图解教程.html`（数据库）与 `docs/方案/内涝平台_开发方案与环境配置.html`（完整环境）。

**所有工具装在 D 盘，不占 C 盘空间：**

| 工具 | 安装路径 |
|---|---|
| PostgreSQL 18 | `D:\dev\PostgreSQL\18` |
| Python 3.13 | `D:\dev\Python313` |
| Node.js | `D:\dev\nodejs` |
| Git | `D:\dev\Git` |
| JDK 21 | 已装（Microsoft 版，保持原位） |
| IDEA / VS Code | `D:\dev\JetBrains` / `D:\dev\VSCode` |

### 2. 初始化数据库

```bash
psql -U postgres -c "CREATE DATABASE floodgis;"
psql -U postgres -d floodgis -c "CREATE EXTENSION postgis;"
psql -U postgres -d floodgis -f sql/01_schema.sql
```

### 3. 启动

```bash
# 后端
cd backend && mvnw spring-boot:run

# 前端
cd frontend && npm run dev
```

浏览器打开 http://localhost:5173

---

## 开发进度

按 12 周排期推进，进度记录在 `docs/周记.md`。

| 阶段 | 模块 | 状态 |
|---|---|---|
| W1 | 环境搭建 | 进行中 |
| W3 | M1 数据采集 | 待开始 |
| W4 | M2 空间处理 + M3 建库 | 待开始 |
| W5 | M4 后端接口 | 待开始 |
| W6 | M5 风险模型 | 待开始 |
| W7–9 | M6 三维可视化 | 待开始 |
| W10–11 | M7 部署交付 | 待开始 |

---

## 数据来源

| 数据 | 来源 | 说明 |
|---|---|---|
| 降雨数据 | 公开气象 API | 需自行申请免费 key |
| DEM 高程 | SRTM 30m | 公开免费，覆盖全国 |
| 路网/水系 | OpenStreetMap | Geofabrik 按省打包 |
| 行政区边界 | 公开 GeoJSON | 需注意坐标系 |

---

## 注意

- 本项目为个人作品集项目，不涉及任何涉密或内部数据
- 地图展示遵守国家地图数据合规要求，底图使用合规来源
- `data/` 目录下的原始数据体积大，不纳入版本控制
