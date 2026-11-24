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
| 数据处理 | Python + GeoPandas + Rasterio + SciPy | 3.13 |
| 空间数据库 | PostgreSQL + PostGIS | 18.7 + 3.7 |
| 后端 | Spring Boot + JdbcTemplate | 3.5.16 / JDK 21 |
| 前端 | Vue 3 + Vite | 3.5 + 7.3 |
| 三维引擎 | Cesium | 1.147 |

---

## 目录结构

```
urban-flood-gis/
├── README.md                 本文件
├── .gitignore                版本控制忽略规则
├── docs/                     项目文档
│   ├── 方案/                 开发方案、排期
│   ├── 教程/                 环境安装图解、代码讲解
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
│   ├── init-db.bat           初始化数据库
│   ├── build-demo.bat        打包本机演示包（前端 dist 打进 jar）
│   ├── 启动演示.bat          双击即可演示，无需服务器
│   └── 演示包说明.txt        演示包的使用与排错说明
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
# 后端（8080）
cd backend && mvnw spring-boot:run

# 前端（5173，/api 代理到后端）
cd frontend && npm run dev
```

浏览器打开 **http://127.0.0.1:5173**

> 别用 `localhost`：天地图 key 的域名白名单只登记了 `127.0.0.1`，换成 `localhost` 会整片返回 403，现象是底图消失、只剩一个蓝色地球。

### 4. 不装环境也能看

`scripts/启动演示.bat` 直接起打包好的演示服务（jar 内已含前端），只要本机有 JDK 21 和 PostgreSQL。使用与排错见 `scripts/演示包说明.txt`。

---

## 开发进度

按 12 周排期推进，周记见 `docs/周记.md`。七个模块主体已全部打通，剩下交付收尾。

| 模块 | 内容 | 状态 | 关键产物 |
|---|---|---|---|
| W1 | 环境搭建 | 已完成 | 11 项工具就绪；`floodgis` 库 5 表 + 6 索引（含 GiST）+ 2 视图 |
| M1 | 数据采集 | 已完成 | Copernicus GLO-30 → 武汉米制 DEM（5162×4440，30 m，EPSG:4547）；区级行政边界；2020 汛期逐日降雨；40 个真实易涝点 |
| M2 | 空间处理 | 已完成 | 填洼 / 坡度 / 汇流累积三张派生栅格，与 DEM 严格同网格 |
| M3 | 建库 + 易涝指数 | 已完成 | 多因子加权易涝指数；水体掩膜（相对地形判据，陆地 7,118 km² / 水体 1,455 km²）；权重敏感性分析 |
| M4 | 后端 + 交互 | 已完成 | `/api/health`、`/api/zones`、`/api/flood-index`、`/api/admin`；屏幕像素 → 栅格像元点击查询；矩形框选统计 |
| M5 | 风险模型 | 已完成 | 日降雨五态马尔科夫链；汛期 153 帧动态风险，2020-05-01 ~ 09-30 |
| M6 | 三维可视化 | 已完成 | Cesium 三维地球 + 天地图影像注记；易涝指数贴图；时间轴按需加载回放（同时在显存里的图层最多 5 个） |
| M7 | 部署交付 | 进行中 | 本机一键演示包跑通（`release/floodgis-demo.jar` + 双击 bat）；待补演示视频与项目说明 |

**几个可以拿出来讲的数字**

- 汛期峰值 2020-07-07：极高风险 1,890.7 km²、高风险 990.0 km²
- 40 个官方易涝点里 39 个拿到指数；官方高风险组指数均值 0.502、全城分位中位 84.9%
- 其中 2 个点原本被水体掩膜误判成水面，改用连通域面积门槛后玉龙路邹家大湾回到 0.641（全城 p95.0%，等级「高」）

---

## 数据来源

| 数据 | 来源 | 说明 |
|---|---|---|
| DEM 高程 | Copernicus DEM GLO-30 | 欧空局 2020 发布，AWS 开放数据托管，免费免 key，1″ 格网（约 30 m） |
| 日降雨 | Open-Meteo Archive（ERA5 再分析） | 免费免 key，取武汉 5 个格点 2017–2024 逐日序列 |
| 行政区边界 | 阿里云 DataV.GeoAtlas | adcode 420100 含下级区县，标准 GeoJSON |
| 易涝点 | 武汉市水务局《中心城区易渍水点风险图》（2020-05） | 59 处中地名可定位的 40 处入库，坐标手工定位，精度约 ±300 m |
| 影像底图 | 天地图 | 个人实名 key，需把访问域名加进白名单 |

---

## 注意

- 本项目为个人作品集项目，不涉及任何涉密或内部数据
- 地图展示遵守国家地图数据合规要求，底图使用合规来源
- `data/` 目录下的原始数据体积大，不纳入版本控制
