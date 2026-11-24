#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
export_static.py —— 把「线上演示」需要的数据导出成静态文件
===============================================================================
线上发布用的是静态托管（没有 JVM，也没有 PostgreSQL），所以四个接口要变成文件：

    /api/health            -> static-data/health.json
    /api/zones             -> static-data/zones.geojson
    /api/admin?level=district -> static-data/admin.geojson
    /api/flood-index?...   -> static-data/tiles/{tx}_{ty}.bin  （点查）
    /api/flood-index/stats -> static-data/stats-index.bin      （框选）

为什么要分块（tiles）
------------------------------------------------------------------------------
点击查询的本质是"算一个偏移量、读 8 个字节"。整张 1656×1671 的网格是 44 MB
（float32 × 4 通道），一次性下载显然不行；按 0.1° 切成 210 块、每块约 115 KB，
点哪儿下哪块，同一块第二次点就不用再下。

为什么量化成 uint16
------------------------------------------------------------------------------
索引 0~1、高程 0~900 m、坡度 0~90°、汇流取 log10(acc+1) 0~8，都用 uint16 表示。
量化误差：高程 0.014 m、坡度 0.0014°、索引 1/65534——比界面显示的小数位还细。
体积从 16 B/格降到 8 B/格，全城 24 MB，分块后每次点击只取 115 KB。

    ⚠ 注意：量化只发生在**导出层**。pipeline 里的计算、后端读的 .bin、
      文档里所有数字都不受影响，这里只是给浏览器一份"够用的副本"。

框选统计为什么单独存一份
------------------------------------------------------------------------------
框选要算"各类面积占多少"、均值中位数分位数，得把框内每一格都读一遍。
点查分块是为"读一格"设计的，读一万格就要发一万次请求，所以另存一份整幅的
索引网格：同样的 3 角秒分辨率、uint8 量化（1/254），约 2.8 MB，
gzip 后 1 MB 出头，一次下载之后全在内存里算。
精度损失只有 0.004，比界面显示的小数位还细，面积和分档占比与后端逐格
统计的差别在显示精度上看不出来，所以界面上不加「近似」标注。

用法
------------------------------------------------------------------------------
    # 1. 先起后端（要它的 zones / admin / health 三份 JSON）
    #    java -jar release/floodgis-demo.jar
    # 2. 导出
    cd pipeline && python publish/export_static.py
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "public" / "static-data"
GRID_JSON = ROOT / "data" / "processed" / "dem" / "wuhan_flood_index_grid.json"
GRID_BIN = ROOT / "data" / "processed" / "dem" / "wuhan_flood_index_grid.bin"
API = "http://127.0.0.1:8080"

TILE_CELLS = 120          # 每块 120 格 = 0.1°
NO_DATA = 0xFFFF          # 点查通道的哨兵值：这个格子没有数据
STATS_SCALE = 254         # 统计网格用 uint8，0~254 表示索引，255 表示无数据

# 量化区间。acc 用 log1p 是因为它跨 7 个数量级，线性量化等于把低值全压成 0。
SCALES = {
    "index": {"type": "linear", "min": 0.0, "max": 1.0},
    "elev":  {"type": "linear", "min": 0.0, "max": 900.0},
    "slope": {"type": "linear", "min": 0.0, "max": 90.0},
    "acc":   {"type": "log1p",  "min": 0.0, "max": 8.0},
}


def api_json(path: str):
    with urllib.request.urlopen(API + path, timeout=20) as r:
        return json.load(r)


def quantize(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """线性量化到 uint16；NaN 留给调用方填哨兵值。"""
    x = (v - lo) / (hi - lo)
    x = np.clip(np.nan_to_num(x, nan=0.0), 0.0, 1.0)
    return np.round(x * (NO_DATA - 1)).astype(np.uint16)


def export_tiles(stack: np.ndarray, width: int, height: int) -> tuple[int, int]:
    """按 TILE_CELLS 切块；边缘不足的部分用哨兵值补齐，前端不用处理变长块。"""
    tiles_dir = OUT / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    for old in tiles_dir.glob("*.bin"):
        old.unlink()

    nx = (width + TILE_CELLS - 1) // TILE_CELLS
    ny = (height + TILE_CELLS - 1) // TILE_CELLS
    total = 0
    for ty in range(ny):
        for tx in range(nx):
            r0, c0 = ty * TILE_CELLS, tx * TILE_CELLS
            block = stack[r0:r0 + TILE_CELLS, c0:c0 + TILE_CELLS]
            pad = np.full((TILE_CELLS, TILE_CELLS, stack.shape[2]), NO_DATA, dtype=np.uint16)
            pad[:block.shape[0], :block.shape[1]] = block
            path = tiles_dir / f"{tx}_{ty}.bin"
            # 小端序：浏览器 DataView 读的时候不用再判字节序
            pad.astype("<u2").tofile(path)
            total += path.stat().st_size
    print(f"  点查网格：{nx}×{ny} = {nx * ny} 块，共 {total / 1048576:.1f} MB")
    return nx, ny


def export_stats(index: np.ndarray, width: int, height: int,
                 bbox: list[float]) -> dict:
    """
    框选统计用的索引网格：整幅 3 角秒、uint8 量化，2.8 MB。

    为什么不用降采样（4×4 求均值）
    --------------------------------------------------------------------------
    第一版是 4 倍降采样，414×418 格只有 340 KB，看着很划算。实测错了：
    一个 4×4 块里只要有一个陆地格子，这块就被当成"有数据"，均值由剩下的
    陆地格子算出来——等于把水体边缘全部填成了低风险陆地。
    全市参与评价的面积因此多算 10%，分档占比偏了 8 个百分点，
    而这种偏差在图上完全看不出来（颜色都对，就是数字不对）。

    所以退回整幅分辨率，只在精度上做文章：索引 0~1 用 uint8 表示，
    分辨率 1/254 = 0.004，比分级阈值之间的间隔小一个数量级；
    体积 1656×1671 ≈ 2.8 MB，gzip 后 1 MB 出头，只下一次，之后全在内存里算。
    """
    q = np.full((height, width), 255, dtype=np.uint8)
    ok = ~np.isnan(index)
    q[ok] = np.minimum(
        np.round(np.clip(index[ok], 0.0, 1.0) * STATS_SCALE), STATS_SCALE
    ).astype(np.uint8)
    q.tofile(OUT / "stats-index.bin")

    size_mb = (OUT / "stats-index.bin").stat().st_size / 1048576
    print(f"  框选网格：{width}×{height}（uint8，1/{STATS_SCALE} 分辨率），"
          f"{size_mb:.1f} MB")

    west, south, east, north = bbox
    return {"file": "stats-index.bin", "width": width, "height": height,
            "scale": STATS_SCALE, "no_data": 255,
            "dx": (east - west) / width, "dy": (north - south) / height}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = json.loads(GRID_JSON.read_text(encoding="utf-8"))
    width, height = int(meta["width"]), int(meta["height"])
    bbox = [float(v) for v in meta["bbox_wgs84"]]
    west, south, east, north = bbox

    # 经纬度每格跨度必须分成两个数算，不能共用一个 res。
    # 原因：bbox 是网格外接矩形，width×res 和 height×res 都等于整跨度只有
    # 在"跨度和格数严格成比例"时才成立——实际 north-south=1.392128°、
    # height=1671，一除是 0.000833110°，而 east-west/width 正好 1/1200=0.000833333°。
    # 差 0.03% 看着无所谓，乘上 1671 行就是半格多，和 Java 的 rowOf() 差一行，
    # 同一点在前后端会落到相邻格子里（高程差十几米，等级都变了）。
    # Java 那边就是 (east-west)/width、(north-south)/height 分开算的，这里照抄。
    dx = (east - west) / width
    dy = (north - south) / height

    print("[1/4] 读网格并量化 ...")
    raw = np.fromfile(GRID_BIN, dtype="<f4")
    if raw.size != width * height * 4:
        raise SystemExit(f"网格尺寸对不上：{raw.size} != {width}×{height}×4")
    arr = raw.reshape(height, width, 4)
    index, elev, slope, acc = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]

    stack = np.full((height, width, 4), NO_DATA, dtype=np.uint16)
    stack[..., 0] = np.where(np.isnan(index), NO_DATA,
                             quantize(index, SCALES["index"]["min"], SCALES["index"]["max"]))
    stack[..., 1] = quantize(elev, SCALES["elev"]["min"], SCALES["elev"]["max"])
    stack[..., 2] = quantize(slope, SCALES["slope"]["min"], SCALES["slope"]["max"])
    with np.errstate(invalid="ignore"):
        logacc = np.log10(np.clip(np.nan_to_num(acc, nan=0.0), 0.0, None) + 1.0)
    stack[..., 3] = quantize(logacc, SCALES["acc"]["min"], SCALES["acc"]["max"])
    print(f"  已量化 {width}×{height}×4 → uint16")

    print("[2/4] 切点查分块 ...")
    nx, ny = export_tiles(stack, width, height)

    print("[3/4] 生成框选统计网格 ...")
    stats_meta = export_stats(index, width, height, bbox)

    print("[4/4] 取三类 JSON 与元数据 ...")
    zones = api_json("/api/zones")
    admin = api_json("/api/admin?level=district")
    (OUT / "zones.geojson").write_text(
        json.dumps(zones, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "admin.geojson").write_text(
        json.dumps(admin, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  易涝点 {len(zones.get('features', []))} 个，"
          f"{(OUT / 'zones.geojson').stat().st_size / 1024:.0f} KB；"
          f"边界 {len(admin.get('features', []))} 个，"
          f"{(OUT / 'admin.geojson').stat().st_size / 1024:.0f} KB")

    # 静态版没有数据库，health.json 明说自己是快照，不伪装成"数据库 UP"
    health = {
        "service": "urban-flood-gis",
        "status": "UP",
        "mode": "static",
        "zoneCount": len(zones.get("features", [])),
        "dataset": "2020 汛期 · 静态快照",
        "postgis": "—",
        "database": "—",
        "time": meta.get("generated_at", ""),
    }
    (OUT / "health.json").write_text(
        json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")

    out_meta = {
        "bbox": bbox,
        "width": width,
        "height": height,
        "dx": dx,
        "dy": dy,
        "tile_cells": TILE_CELLS,
        "tiles_x": nx,
        "tiles_y": ny,
        "no_data": NO_DATA,
        "levels": meta["levels"],
        "channels": [{"name": k, **v} for k, v in SCALES.items()],
        "stats": stats_meta,
        "note": "静态快照：由 pipeline/publish/export_static.py 从本地网格导出，"
                "点查按 0.1° 分块（uint16），框选用整幅 uint8 索引网格",
    }
    (OUT / "grid-meta.json").write_text(
        json.dumps(out_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"\n完成：{OUT}")
    print(f"  合计 {total / 1048576:.1f} MB")


if __name__ == "__main__":
    main()
