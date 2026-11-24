# -*- coding: utf-8 -*-
"""水体判据二维扫描：填深门槛 × 面积门槛。

要同时满足三件事：
  1. 真湖不能被误删（用 6 个湖心点守）
  2. 小面积封闭洼地要被放出来（玉龙路 0.583 km²、竹叶山 0.325 km²）
  3. 水体总面积不能剧烈变化（否则整张指数图都要重估）
"""
import json
import sys
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\wb\urban-flood-gis")
sys.path.insert(0, str(PROJECT / "pipeline" / "process"))
DEM_DIR = PROJECT / "data" / "processed" / "dem"

LAKES = [
    ("东湖", 114.3720, 30.5560),
    ("沙湖", 114.3307, 30.5747),
    ("汤逊湖", 114.3420, 30.4280),
    ("梁子湖", 114.5300, 30.2500),
    ("后官湖", 114.1230, 30.5270),
    ("南湖", 114.3355, 30.4885),
]


def main():
    import rasterio
    import scipy.ndimage as ndi
    from pyproj import Transformer

    import flood_index as fi

    with rasterio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
        transform, crs, w, h = ds.transform, ds.crs, ds.width, ds.height

    elev = np.asarray(np.load(DEM_DIR / "wuhan_dem_cgcs2000.npy", mmap_mode="r"))
    slope = np.asarray(np.load(DEM_DIR / "wuhan_slope.npy", mmap_mode="r"))
    fill = np.asarray(np.load(DEM_DIR / "wuhan_dem_filldepth.npy", mmap_mode="r"))
    acc = np.asarray(np.load(DEM_DIR / "wuhan_flowacc.npy", mmap_mode="r"))

    valid = np.asarray(np.isfinite(elev) & np.isfinite(slope) & np.isfinite(acc)
                       & fi.city_mask((h, w), transform))
    city_km2 = valid.sum() * 900 / 1e6

    to_xy = Transformer.from_crs(4326, crs, always_xy=True)

    def rc(lon, lat):
        x, y = to_xy.transform(lon, lat)
        col, row = ~transform * (x, y)
        return int(row), int(col)

    pts = json.load(open(PROJECT / "data/clean/flood_zones_candidates.json", encoding="utf-8"))
    pt_rc = [(p["name"], p["grade"]) + rc(p["lon"], p["lat"]) for p in pts]
    lake_rc = [(n,) + rc(lon, lat) for n, lon, lat in LAKES]

    print(f"市域 {city_km2:,.0f} km²")
    print()
    hdr = (f"{'填深>':>6}{'面积>=':>8}{'水体km²':>10}{'占%':>7}{'陆地km²':>10}"
           f"{'域数':>6}{'湖心全是水':>11}{'被删的湖':>22}{'判为水的易涝点':>6}")
    print(hdr)
    print("-" * len(hdr.encode('gbk', errors='ignore')))

    for fmin in (3.0, 3.5, 4.0, 4.5, 5.0):
        cand = valid & (slope < fi.WATER_SLOPE_MAX) & (fill > fmin) & (elev < fi.WATER_ELEV_MAX)
        labels, n = ndi.label(cand, structure=np.ones((3, 3), dtype=int))
        if n == 0:
            continue
        areas = np.bincount(labels.ravel(), minlength=n + 1)[1:] * 900 / 1e6
        for amin in (0.3, 1.0):
            kept = np.zeros(n + 1, dtype=bool)
            kept[1:] = areas >= amin
            wt = areas[kept[1:]].sum()

            def is_water(row, col):
                sub = labels[max(0, row - 6):row + 7, max(0, col - 6):col + 7]
                v = sub[sub > 0]
                if v.size == 0:
                    return False
                lab = int(np.bincount(v).argmax())
                return bool(kept[lab])

            lake_ok = [nm for nm, r, c in lake_rc if is_water(r, c)]
            lake_bad = [nm for nm, r, c in lake_rc if not is_water(r, c)]
            # 易涝点：用单像元判定，和 probe 的口径一致
            n_pt = 0
            for nm, g, r, c in pt_rc:
                if is_water(r, c):
                    n_pt += 1
            print(f"{fmin:>6.1f}{amin:>8.1f}{wt:>10.1f}{wt/city_km2*100:>7.2f}"
                  f"{city_km2-wt:>10.0f}{int(kept.sum()):>6}"
                  f"{len(lake_ok)}/{len(lake_rc):>9}"
                  f"{('、'.join(lake_bad) if lake_bad else '无'):>22}{n_pt:>6}")


if __name__ == "__main__":
    main()
