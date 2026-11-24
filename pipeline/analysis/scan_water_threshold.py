# -*- coding: utf-8 -*-
"""水体判据改进：面积门槛扫描。

目的：定一个面积门槛，把「小面积封闭洼地」和「真水面」分开。
原则：水体只指大面积的、有出水口的永久水面；小洼地恰恰是内涝点本身，
不该被当成水剔掉。

输出：不同门槛下的水体总面积、连通域个数、以及 40 个易涝点里有几个仍被判为水。
"""
import json
import sys
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\wb\urban-flood-gis")
sys.path.insert(0, str(PROJECT / "pipeline" / "process"))
DEM_DIR = PROJECT / "data" / "processed" / "dem"

import flood_index as fi  # noqa: E402


def main():
    import rasterio
    import scipy.ndimage as ndi
    from pyproj import Transformer

    with rasterio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
        transform, crs, w, h = ds.transform, ds.crs, ds.width, ds.height

    elev = np.asarray(np.load(DEM_DIR / "wuhan_dem_cgcs2000.npy", mmap_mode="r"))
    slope = np.asarray(np.load(DEM_DIR / "wuhan_slope.npy", mmap_mode="r"))
    fill = np.asarray(np.load(DEM_DIR / "wuhan_dem_filldepth.npy", mmap_mode="r"))
    acc = np.asarray(np.load(DEM_DIR / "wuhan_flowacc.npy", mmap_mode="r"))

    valid_all = np.isfinite(elev) & np.isfinite(slope) & np.isfinite(acc)
    m_city = fi.city_mask((h, w), transform)
    valid = np.asarray(valid_all & m_city)
    city_km2 = valid.sum() * 900 / 1e6

    cand = (valid & (slope < fi.WATER_SLOPE_MAX) & (fill > fi.WATER_FILL_MIN)
            & (elev < fi.WATER_ELEV_MAX))
    labels, n = ndi.label(cand, structure=np.ones((3, 3), dtype=int))
    areas = np.bincount(labels.ravel(), minlength=n + 1)[1:] * 900 / 1e6
    print(f"市域有效 {city_km2:,.0f} km²；候选连通域 {n} 个，候选总面积 {areas.sum():,.0f} km²")

    print()
    print("=" * 92)
    print("面积分布（候选连通域）")
    print("=" * 92)
    bins = [0, 0.3, 0.5, 1, 2, 5, 10, 50, 100, 1e9]
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (areas >= lo) & (areas < hi)
        if m.sum():
            print(f"  {lo:>6g} ~ {hi if hi < 1e9 else float('inf'):>6g} km²: "
                  f"{int(m.sum()):>5} 个，合计 {areas[m].sum():>9.1f} km²")

    # 40 个易涝点
    cand_pts = json.load(open(PROJECT / "data/clean/flood_zones_candidates.json", encoding="utf-8"))
    to_xy = Transformer.from_crs(4326, crs, always_xy=True)

    def label_at(lon, lat):
        x, y = to_xy.transform(lon, lat)
        col, row = ~transform * (x, y)
        col, row = int(col), int(row)
        if not (0 <= row < h and 0 <= col < w):
            return 0
        sub = labels[max(0, row - 2):row + 3, max(0, col - 2):col + 3]
        vals = sub[sub > 0]
        return int(np.bincount(vals).argmax()) if vals.size else 0

    pt_labels = []
    for r in cand_pts:
        lab = label_at(r["lon"], r["lat"])
        pt_labels.append((r["name"], r["grade"], lab, areas[lab - 1] if lab else 0.0))

    print()
    print("=" * 92)
    print("易涝点落区：哪些点所在的候选域面积偏小（= 可能是封闭洼地被误判）")
    print("=" * 92)
    print(f"{'名称':<20}{'官方':<6}{'域编号':>8}{'域面积km²':>12}")
    for name, grade, lab, a in sorted(pt_labels, key=lambda x: x[3] if x[3] else 0):
        if lab == 0:
            print(f"{name:<20}{grade:<6}{'—':>8}{'不在候选域':>12}")
        elif a < 10:
            print(f"{name:<20}{grade:<6}{lab:>8}{a:>12.3f}   ← 面积偏小")

    print()
    print("=" * 92)
    print("门槛扫描")
    print("=" * 92)
    print(f"{'门槛km²':>9}{'保留域数':>10}{'水体km²':>11}{'占市域%':>10}{'陆地km²':>12}"
          f"{'被判为水的易涝点':>20}")
    for th in (0.3, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
        keep = areas >= th
        wt = areas[keep].sum()
        # 一个像元若在某域里且该域被保留，才算水
        kept_labels = np.zeros(n + 1, dtype=bool)
        kept_labels[1:] = keep
        water = kept_labels[labels]
        n_pt = sum(1 for _, _, lab, _ in pt_labels if lab and kept_labels[lab])
        print(f"{th:>9.1f}{int(keep.sum()):>10}{wt:>11.1f}{wt/city_km2*100:>10.2f}"
              f"{city_km2-wt:>12.0f}{n_pt:>20}")
        del water, kept_labels

    # 各阈值下具体是哪些点
    print()
    for th in (1.0, 2.0, 3.0):
        kept = np.zeros(n + 1, dtype=bool)
        kept[1:] = areas >= th
        kept_pts = [name for name, _, lab, _ in pt_labels if lab and kept[lab]]
        print(f"门槛 {th} km² 下仍被判为水的易涝点：{kept_pts if kept_pts else '无'}")


if __name__ == "__main__":
    main()
