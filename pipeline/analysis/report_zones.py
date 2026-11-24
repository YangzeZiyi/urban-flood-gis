# -*- coding: utf-8 -*-
"""导出易涝点对照新表：水体判据改成「面积 >1 km²」前后的指数变化。

读三个东西：
  1. data/clean/_zones_before_water_v2.json   改判据之前的体检结果（旧指数在里面）
  2. frontend/public/overlay/wuhan_flood_index_grid.json/.bin  新的点击网格
  3. 同目录的 levels 分档

输出：
  - 控制台打印对照表
  - data/clean/zones_index_compare.csv
"""
import json
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\wb\urban-flood-gis")
OL = PROJECT / "frontend" / "public" / "overlay"      # 贴图（给前端）
GRID = PROJECT / "data" / "processed" / "dem"         # 点击网格（给后端，不进 public）
OLD = PROJECT / "data" / "clean" / "_zones_before_water_v2.json"
OUT_CSV = PROJECT / "data" / "clean" / "zones_index_compare.csv"


def main():
    meta = json.loads((GRID / "wuhan_flood_index_grid.json").read_text(encoding="utf-8"))
    w, h = meta["width"], meta["height"]
    grid = np.memmap(GRID / meta["file"], dtype="<f4", mode="r", shape=(h, w, 4))

    west, south, east, north = meta["bbox_wgs84"]
    index = np.asarray(grid[:, :, 0])
    land = index[np.isfinite(index)]
    land_sorted = np.sort(land)
    print(f"新网格 {w}x{h}，陆地像元 {land.size:,}，"
          f"均值 {land.mean():.4f} 中位 {np.median(land):.4f} p97 {np.percentile(land, 97):.4f}")

    levels = meta["levels"]

    def level_of(v):
        if not np.isfinite(v):
            return "水体/无值"
        for lv in levels:
            if lv["lo"] <= v < lv["hi"]:
                return lv["name"]
        return levels[-1]["name"]

    def pct_of(v):
        if not np.isfinite(v):
            return float("nan")
        return float(np.searchsorted(land_sorted, v) / land_sorted.size * 100)

    old = json.loads(OLD.read_text(encoding="utf-8"))
    rows = []
    for r in old:
        lon, lat = r["lon"], r["lat"]
        col = int((lon - west) / (east - west) * w)
        row = int((north - lat) / (north - south) * h)
        new = float(grid[row, col, 0]) if (0 <= row < h and 0 <= col < w) else float("nan")
        old_v = r.get("index")
        old_v = float(old_v) if isinstance(old_v, (int, float)) else float("nan")
        rows.append({
            "name": r["name"], "grade": r["grade"], "district": r.get("actual", [""])[0],
            "old": old_v, "new": new, "pct": pct_of(new), "level": level_of(new),
        })

    # ---- 控制台对照表 ----
    print()
    print("=" * 104)
    print("40 个官方易涝点：水体判据改动前后的指数对照")
    print("（排序：官方高风险在前，同级按新指数降序）")
    print("=" * 104)
    print(f"{'名称':<20}{'官方':<5}{'区':<7}{'旧指数':>9}{'新指数':>9}"
          f"{'新分位':>8}{'新等级':>8}{'变化':>9}")
    print("-" * 104)
    gorder = {"高": 0, "中": 1, "低": 2}
    for r in sorted(rows, key=lambda x: (gorder.get(x["grade"], 9),
                                         -(x["new"] if np.isfinite(x["new"]) else -1))):
        if np.isfinite(r["old"]) and np.isfinite(r["new"]):
            delta = f"{r['new'] - r['old']:+.3f}"
        elif not np.isfinite(r["old"]) and np.isfinite(r["new"]):
            delta = "新增"
        else:
            delta = "仍无值"
        print(f"{r['name']:<20}{r['grade']:<5}{r['district']:<7}"
              f"{(f'{r[chr(111)+chr(108)+chr(100)]:.3f}' if np.isfinite(r['old']) else '—'):>9}"
              f"{(f'{r[chr(110)+chr(101)+chr(119)]:.3f}' if np.isfinite(r['new']) else '—'):>9}"
              f"{(f'{r[chr(112)+chr(99)+chr(116)]:.1f}%' if np.isfinite(r['pct']) else '—'):>8}"
              f"{r['level']:>8}{delta:>9}")

    # ---- 分组统计 ----
    print()
    print("=" * 104)
    print("分组对照（只统计有指数的点）")
    print("=" * 104)
    print(f"{'组':<22}{'可评价数':>8}{'旧均值':>9}{'旧中位':>9}{'新均值':>9}{'新中位':>9}{'新分位中位':>11}")
    land_mean = land.mean()
    print(f"{'全城陆地（对照）':<22}{land.size:>8,}{'':>9}{'':>9}{land_mean:>9.3f}"
          f"{np.median(land):>9.3f}{50.0:>10.1f}%")
    for g in ("高", "中"):
        sel = [r for r in rows if r["grade"] == g]
        o = [r["old"] for r in sel if np.isfinite(r["old"])]
        n = [r["new"] for r in sel if np.isfinite(r["new"])]
        p = [r["pct"] for r in sel if np.isfinite(r["pct"])]
        om = f"{np.mean(o):.3f}" if o else "—"
        omed = f"{np.median(o):.3f}" if o else "—"
        print(f"{'官方' + g + '风险':<22}{len(n):>8}{om:>9}{omed:>9}"
              f"{np.mean(n):>9.3f}{np.median(n):>9.3f}{(np.median(p) if p else 0):>10.1f}%")

    # ---- CSV ----
    import csv
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["名称", "官方等级", "行政区", "旧指数", "新指数", "新分位%", "新等级", "变化"])
        for r in sorted(rows, key=lambda x: (gorder.get(x["grade"], 9),
                                             -(x["new"] if np.isfinite(x["new"]) else -1))):
            if np.isfinite(r["old"]) and np.isfinite(r["new"]):
                d = round(r["new"] - r["old"], 4)
            elif not np.isfinite(r["old"]) and np.isfinite(r["new"]):
                d = "新增（原判为水）"
            else:
                d = "仍无值（判为水）"
            wr.writerow([r["name"], r["grade"], r["district"],
                         round(r["old"], 4) if np.isfinite(r["old"]) else "无值",
                         round(r["new"], 4) if np.isfinite(r["new"]) else "无值",
                         round(r["pct"], 1) if np.isfinite(r["pct"]) else "无值",
                         r["level"], d])
    print()
    print(f"已写 {OUT_CSV.relative_to(PROJECT)}（{len(rows)} 行）")


if __name__ == "__main__":
    main()
