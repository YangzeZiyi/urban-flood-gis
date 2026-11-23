#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_dem.py —— 采集 Copernicus DEM 30m 原始分幅，拼接裁剪，重投影成米制
===============================================================================

为什么要用 Copernicus DEM，而不是网上那种"打包好的 DEM"？
--------------------------------------------------------------------------
因为原始产品才有完整的处理链条可讲、可复现：
    分幅下载 -> 按范围拼接裁剪 -> 投影转换 -> 质量核查
这四步是测绘/遥感数据工程的标准动作。跳过它们直接用别人裁好的图，
一旦遇到"数据对不上""面积算错""坡度全是 0"这类问题，你无从下手。

数据源：Copernicus DEM GLO-30（欧空局 2020 年发布）
    https://copernicus-dem-30m.s3.amazonaws.com/
    AWS Open Data 托管，免费、无需注册、无需 key。
    这是目前公开可得的**质量最高**的全球 30 米 DEM：
      - 水平基准 WGS84，垂直基准 EGM2008 大地水准面
      - 1 角秒格网 = 赤道处约 30 米
      - 融合了 TanDEM-X 雷达干涉测量数据，山区比 SRTM 稳得多

分幅规则（这是最容易搞错的地方）
--------------------------------------------------------------------------
每幅固定 1° x 1°，文件名叫：
    Copernicus_DSM_COG_10_N30_00_E114_00_DEM.tif
                            ↑纬度  ↑经度
    N30 表示 30°N~31°N，E114 表示 114°E~115°E。
武汉跨 113.7~115.1°E、29.97~31.36°N，所以需要 3x3 = 9 幅（见运行输出）。

关于坐标系的重要决策
--------------------------------------------------------------------------
原始分幅是 EPSG:4326（经纬度）。但**经纬度不能直接算坡度**：
    经度 1° 在赤道是 111 km，在 30°N 只有 96.5 km；
    纬度 1° 一直是 111 km。
    格网在物理上不是正方形，算出来的坡度、流向全是错的。
所以必须重投影到**等角投影**，让格网变成真正的正方形。

选 EPSG:4547「CGCS2000 / 3 度带高斯-克吕格，中央经线 114°E」：
    - CGCS2000 是中国国家大地坐标系，与 WGS84 差异在厘米级
    - 3 度带是中国大比例尺测图的国标分带
    - 中央经线 114°E 正好穿过武汉（113.70~115.08，最大偏离 1.08°），
      投影长度变形 < 0.02%，工程上可忽略
    - 单位是米，坡度、汇流、距离都能直接算

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python fetch/fetch_dem.py              # 下载 + 拼接 + 重投影
    python fetch/fetch_dem.py --dry-run    # 只看需要哪几幅，不下载
    python fetch/fetch_dem.py --force      # 忽略已有分幅，重新下载

依赖：rasterio（含 GDAL）、pyproj、numpy、psycopg2、requests
===============================================================================
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import psycopg2
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.warp import calculate_default_transform, reproject

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
# 让 print 立即输出到屏幕。
# Python 的 stdout 在"被管道/重定向接管"时默认按 4KB 块缓冲，
# 下载 400MB 要跑几分钟，这期间屏幕上会一片空白，看起来像卡死。
# 改成行缓冲后，每打印一行就立刻可见。
sys.stdout.reconfigure(line_buffering=True)

PIPELINE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = PIPELINE_DIR.parent
ENV_FILE = PIPELINE_DIR / ".env"

RAW_DIR = PROJECT_DIR / "data" / "raw" / "dem" / "copernicus"
OUT_DIR = PROJECT_DIR / "data" / "processed" / "dem"
OUT_4326 = OUT_DIR / "wuhan_dem_4326.tif"
OUT_PROJ = OUT_DIR / "wuhan_dem_cgcs2000.tif"
META_JSON = OUT_DIR / "wuhan_dem_meta.json"

S3_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
TILE_URL = S3_BASE + "/{name}/{name}.tif"

# 1 角秒 = 1/3600 度，这就是 GLO-30 的格网大小
ARCSEC = 1.0 / 3600.0

# 目标投影与格网大小（米）
DST_CRS = "EPSG:4547"
DST_RES_M = 30.0

# Copernicus DEM 的海绵（void）填充值。
# 注意：单幅内部通常没有空洞，但拼接后边界或水域可能出现，必须显式声明，
# 否则它会被当成真实高程参与统计，把"最低点"算到 -32767 米。
NODATA = -32767.0

# 并发下载数。S3 是「按连接限速」而不是「按 IP 限总带宽」，
# 实测单连接 1.2 MB/s、4 并发合计 4 MB/s，所以并发能显著提速。
MAX_WORKERS = 4


# ---------------------------------------------------------------------------
# 第 1 步：读取数据库配置
# ---------------------------------------------------------------------------
def load_env(path: Path) -> None:
    """把 .env 的 KEY=VALUE 读进 os.environ，psycopg2 就能免参数连接。

    和 fetch_admin.py 里是同一套逻辑，刻意保持一致：
    数据管线的每个脚本都从同一个 .env 取配置，改密码只改一处。
    """
    if not path.exists():
        sys.exit(f"[错误] 找不到 {path}\n       请把 pipeline/.env.example 复制为 .env 并填密码")

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


# ---------------------------------------------------------------------------
# 第 2 步：从数据库取武汉的经纬度范围
# ---------------------------------------------------------------------------
def get_wuhan_bbox() -> tuple[float, float, float, float]:
    """查 admin_boundary 里市级那一行，返回 (minx, miny, maxx, maxy)。

    为什么要从数据库读，而不是把范围写死在脚本里？
        因为范围是**上一步算出来的**。写死的话，哪天换了城市、
        或者边界数据更新了，这里不会跟着变，而你很难想起来改它。
        ——管线的每一段都应该吃上一段的输出，而不是各写各的常量。

    ST_Extent 返回的是 box2d（外接矩形），ST_XMin 这类函数从里面取四个边。
    用子查询是因为 ST_Extent 是聚合函数，不能直接和其他列一起选。
    """
    sql = """
        SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
        FROM (SELECT ST_Extent(geom) AS e
              FROM admin_boundary
              WHERE level = 'city') AS s
    """
    with psycopg2.connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()

    if not row or row[0] is None:
        sys.exit("[错误] admin_boundary 里没有 level='city' 的记录，请先跑 fetch_admin.py")

    return tuple(float(v) for v in row)


# ---------------------------------------------------------------------------
# 第 3 步：算出需要哪几幅
# ---------------------------------------------------------------------------
def tile_name(lat: int, lon: int) -> str:
    """把整数经纬度转成分幅文件名。

    南纬用 S、西纬用 W。武汉全是 N/E，但写成通用逻辑，
    以后换到别的城市不会突然出错。
    """
    ns = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


def tiles_for_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """把经纬度范围展开成"整数度格网"列表。

    一幅图覆盖 [lon, lon+1) x [lat, lat+1)，所以：
        下界用 floor，上界也用 floor（不是 ceil）。
    举例：maxy = 31.3613 -> floor = 31 -> 需要 N31 那一幅（覆盖 31~32）。
    如果 maxy 正好是整数（比如 31.0），floor 会多取一幅，
    只多花几十 MB，不影响正确性——宁可多取，不可漏取。
    """
    minx, miny, maxx, maxy = bbox
    lats = range(math.floor(miny), math.floor(maxy) + 1)
    lons = range(math.floor(minx), math.floor(maxx) + 1)
    return [(la, lo) for la in lats for lo in lons]


# ---------------------------------------------------------------------------
# 第 4 步：下载（curl + 断点续传 + 并发）
# ---------------------------------------------------------------------------
def head_size(url: str) -> int:
    """问服务器这个文件有多大（字节）。用于校验下载是否完整。

    用 HEAD 而不是 GET：只要响应头，不传内容，几十字节就够。
    """
    try:
        r = requests.head(url, timeout=20, allow_redirects=True)
        return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


def download_one(lat: int, lon: int, force: bool = False) -> tuple[str, Path, float]:
    """下载单幅，返回 (名字, 路径, 耗时秒)。

    为什么用 curl 而不是 Python 的 requests？
        实测同一条线路，requests 下载 COP-DEM 只有 0.07 MB/s，
        curl 能到 1.2~11 MB/s，差 17 倍以上。
        原因是 requests 在这条链路上的 TLS/分块读取效率很差。
        工程上不必纠结"纯 Python 才优雅"——能用系统自带的 curl 就用。

    为什么要断点续传（-C -）？
        400 MB 下到 90% 断网，没有续传就得从头再来。
        curl 会读本地已下载的字节数，让服务器接着发剩下的部分。
    """
    name = tile_name(lat, lon)
    target = RAW_DIR / f"{name}.tif"
    url = TILE_URL.format(name=name)
    part = target.with_suffix(".part")

    if target.exists() and not force:
        return name, target, 0.0

    curl = shutil.which("curl")
    if not curl:
        sys.exit("[错误] 系统里找不到 curl（Windows 10+ 自带 C:\\Windows\\System32\\curl.exe）")

    cmd = [curl, "-L", "--fail", "--silent", "--show-error",
           "--retry", "5", "--retry-delay", "3", "--max-time", "1800",
           "-o", str(part)]
    # 只有 .part 已存在时才加 -C -，否则某些 curl 版本会报 "cannot resume"
    if part.exists() and part.stat().st_size > 0:
        cmd += ["-C", "-"]
    cmd.append(url)

    t0 = time.time()
    subprocess.run(cmd, check=True)

    expected = head_size(url)
    actual = part.stat().st_size
    if expected and actual != expected:
        raise RuntimeError(f"{name} 大小不符：期望 {expected}，实得 {actual}")

    part.replace(target)
    return name, target, time.time() - t0


def download_all(tiles: list[tuple[int, int]], force: bool) -> list[Path]:
    """并发下载全部分幅，边下边打印进度。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    done: dict[str, Path] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(download_one, la, lo, force): (la, lo) for la, lo in tiles}
        for fut in as_completed(futures):
            la, lo = futures[fut]
            try:
                name, path, cost = fut.result()
            except Exception as e:
                sys.exit(f"\n[错误] 下载 N{la}_E{lo} 失败：{e}")
            done[name] = path
            if cost == 0:
                print(f"      = {name}  已存在，跳过")
            else:
                mb = path.stat().st_size / 1048576
                print(f"      ✓ {name}  {mb:.1f} MB  {cost:.1f}s")

    # 按经纬度顺序返回，保证拼接顺序稳定（虽然 merge 不在乎顺序，
    # 但报错时日志可读）
    return [done[tile_name(la, lo)] for la, lo in tiles]


# ---------------------------------------------------------------------------
# 第 5 步：按范围拼接 + 裁剪，输出地理坐标版
# ---------------------------------------------------------------------------
def mosaic_and_clip(paths: list[Path], bbox: tuple[float, float, float, float]) -> Path:
    """把 9 幅拼成一张，并裁到武汉范围。

    rasterio.merge.merge 的关键参数：
        bounds  —— 输出范围。只读各分幅里**与这个范围相交的窗口**，
                   不会把 9 幅 3600x3600 全部读进内存。
        res     —— 输出分辨率。必须显式给 (1/3600, 1/3600)，
                   否则它会沿用第一幅的，遇到分辨率的微小差异会错位。
        nodata  —— 让海绵值透明，拼接缝上不会出现 -32767 的"深坑"。
    """
    datasets = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(
            datasets,
            bounds=bbox,
            res=(ARCSEC, ARCSEC),
            nodata=NODATA,
            dtype="float32",
            resampling=Resampling.bilinear,
        )
    finally:
        for ds in datasets:
            ds.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": mosaic.shape[1],
        "width": mosaic.shape[2],
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": NODATA,
        "compress": "deflate",
        # predictor=2 是"水平差分"预测，对高程这种连续数据能再压掉 20~30%
        "predictor": 2,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "BIGTIFF": "IF_SAFER",
    }
    with rasterio.open(OUT_4326, "w", **profile) as dst:
        dst.write(mosaic[0], 1)
        dst.build_overviews([2, 4, 8, 16], Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")

    return OUT_4326


# ---------------------------------------------------------------------------
# 第 6 步：重投影到 CGCS2000 / 3 度带 CM 114°E
# ---------------------------------------------------------------------------
def reproject_to_metric(src_path: Path) -> Path:
    """把经纬度格网投到米制平面格网。

    calculate_default_transform 会自动算出目标范围和像元数。
    显式传 resolution=30 表示"我要 30 米格网"，否则它会按原像元数推算，
    得到的可能不是整数米。

    重采样选 bilinear（双线性）而不是 nearest（最近邻）：
        nearest 适合**分类数据**（土地利用类型、行政区代码），
        连续数据（高程、温度）必须用 bilinear/cubic，
        否则会出现一块块阶梯，后面算坡度全是断崖。
    """
    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, DST_CRS, src.width, src.height, *src.bounds,
            resolution=DST_RES_M,
        )
        profile = src.profile.copy()
        profile.update(
            crs=DST_CRS,
            transform=transform,
            width=width,
            height=height,
            compress="deflate",
            predictor=2,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="IF_SAFER",
        )

        with rasterio.open(OUT_PROJ, "w", **profile) as dst:
            for band in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=NODATA,
                    dst_transform=transform,
                    dst_crs=DST_CRS,
                    dst_nodata=NODATA,
                    resampling=Resampling.bilinear,
                )
            dst.build_overviews([2, 4, 8, 16], Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")

    return OUT_PROJ


# ---------------------------------------------------------------------------
# 第 7 步：回查验证（写进去 != 写对了）
# ---------------------------------------------------------------------------
def verify_and_report(bbox, tiles, paths) -> dict:
    """把输出读回来核对，并打印一份可存档的统计。"""
    print()
    print("=" * 66)
    print("回查验证（不是看文件存不存在，而是把数据读回来算）")
    print("=" * 66)

    result = {
        "bbox_wgs84": [round(v, 6) for v in bbox],
        "tiles": [tile_name(la, lo) for la, lo in tiles],
        "tile_count": len(tiles),
        "tile_bytes": 0,
        "outputs": {},
    }

    for p in paths:
        if p.exists():
            result["tile_bytes"] += p.stat().st_size
    print(f"原始分幅      : {len(tiles)} 幅，合计 {result['tile_bytes'] / 1048576:.0f} MB")

    for path, label in ((OUT_4326, "地理坐标版"), (OUT_PROJ, "投影坐标版")):
        with rasterio.open(path) as ds:
            data = ds.read(1)
            valid = data != NODATA
            if valid.sum() == 0:
                sys.exit(f"[错误] {path.name} 全是 NODATA")

            vals = data[valid]
            # 用 2%/98% 分位数做稳健范围，避免个别异常值干扰判断
            p2, p98 = np.percentile(vals, [2, 98])
            res_x = abs(ds.transform.a)
            res_y = abs(ds.transform.e)

            info = {
                "file": path.name,
                "size_px": [ds.width, ds.height],
                "crs": str(ds.crs),
                "res": [round(res_x, 8), round(res_y, 8)],
                "elev_min": round(float(vals.min()), 2),
                "elev_max": round(float(vals.max()), 2),
                "elev_mean": round(float(vals.mean()), 2),
                "elev_p2_p98": [round(float(p2), 2), round(float(p98), 2)],
                "valid_ratio": round(float(valid.sum() / valid.size), 4),
                "size_mb": round(path.stat().st_size / 1048576, 1),
            }
            result["outputs"][label] = info

            print(f"\n{label}  {path.name}")
            print(f"  尺寸      : {ds.width} x {ds.height}  ({ds.width * ds.height / 1e6:.1f} 百万像元)")
            print(f"  坐标系    : {ds.crs.to_string()}  ({'经纬度' if ds.crs.is_geographic else '米制投影'})")
            if ds.crs.is_geographic:
                print(f"  分辨率    : {res_x * 3600:.2f} 角秒 = {res_x * 111320:.1f} m（赤道）")
            else:
                print(f"  分辨率    : {res_x:.1f} m x {res_y:.1f} m  ← 真正的正方形格网")
                area_km2 = ds.width * ds.height * (res_x * res_y) / 1e6
                print(f"  覆盖面积  : {area_km2:,.0f} km²（武汉外接矩形，不是市域面积）")
            print(f"  高程范围  : {vals.min():.1f} ~ {vals.max():.1f} m")
            print(f"  平均/中位 : {vals.mean():.1f} / {np.median(vals):.1f} m")
            print(f"  2%~98%    : {p2:.1f} ~ {p98:.1f} m  ← 去掉极端值后的主体分布")
            print(f"  有效像元  : {valid.sum() / valid.size * 100:.2f}%")
            print(f"  文件大小  : {info['size_mb']} MB")

    # 交叉验证：两个版本的统计必须一致（除重采样引入的细微差异）
    g = result["outputs"]["地理坐标版"]
    m = result["outputs"]["投影坐标版"]
    diff = abs(g["elev_mean"] - m["elev_mean"])
    print()
    print(f"两个版本高程均值差异：{diff:.3f} m  "
          f"({'✓ 重投影未引入偏差' if diff < 0.5 else '✗ 差异过大，需检查'})")

    META_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"统计已存档：{META_JSON.name}")
    print("=" * 66)
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="采集 Copernicus DEM 30m 并处理成武汉范围米制 DEM")
    parser.add_argument("--dry-run", action="store_true", help="只列出所需分幅，不下载")
    parser.add_argument("--force", action="store_true", help="忽略已下载的分幅，重新下载")
    args = parser.parse_args()

    load_env(ENV_FILE)
    print(f"[1/6] 已加载数据库配置：{ENV_FILE.name}")

    bbox = get_wuhan_bbox()
    print(f"[2/6] 武汉范围（来自 admin_boundary）：")
    print(f"      经度 {bbox[0]:.4f} ~ {bbox[2]:.4f}  "
          f"（跨度 {(bbox[2] - bbox[0]) * 96.5:.0f} km）")
    print(f"      纬度 {bbox[1]:.4f} ~ {bbox[3]:.4f}  "
          f"（跨度 {(bbox[3] - bbox[1]) * 111:.0f} km）")

    tiles = tiles_for_bbox(bbox)
    print(f"[3/6] 需要 {len(tiles)} 幅 1°x1° 分幅：")
    for la, lo in tiles:
        print(f"      N{la}_E{lo}  ->  {tile_name(la, lo)}.tif")

    if args.dry_run:
        print("\n--dry-run：到此为止，未下载任何文件。")
        return

    print(f"[4/6] 开始下载（{MAX_WORKERS} 并发，走 curl，支持断点续传）")
    print(f"      缓存目录：{RAW_DIR}")
    paths = download_all(tiles, args.force)

    print(f"[5/6] 拼接 + 裁剪 + 重投影")
    print(f"      输出 1：{OUT_4326.name}（EPSG:4326，原始地理坐标）")
    mosaic_and_clip(paths, bbox)
    print(f"      输出 2：{OUT_PROJ.name}（{DST_CRS}，米制 30m 格网）")
    reproject_to_metric(OUT_4326)

    print("[6/6] 回查验证")
    verify_and_report(bbox, tiles, paths)

    print("\n下一步：用这张米制 DEM 算坡度、填洼、汇流累积量（M2 风险模型的地形因子）")


if __name__ == "__main__":
    main()
