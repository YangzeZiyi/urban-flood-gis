"""
水体判据探查：大湖水面在「填深 / 坡度 / 汇流」上的表现
=====================================================================
目的
----
现有水体判据是 `elev <= 22m 且 slope < 0.1°`，它按「绝对高程」划线，
结果东湖（水面 23~24m）、南湖、沙湖全部漏掉——它们的海拔比阈值高一点点，
但物理上明明是水。

要换成「相对地形」的判据，就得先看清一件事：
**湖面和低洼陆地在填深（fill depth）上到底差多少？**

填深的含义：Priority-Flood 把这个格子填到「水能流出去」的高度所需的土方。
- 封闭湖盆：四周高、中间低，填深 = 水面到出水口的落差，通常几米
- 沿江平地：本来就是倾斜的，填深接近 0
- 建筑物围成的假碗（DSM 造成）：填深也有，但坡度不小 / 面积不大

所以「填深大 + 坡度接近 0 + 连片」应该能把真湖面和假洼地分开。
这个脚本就是来验证这个假设的，不写任何生产代码。

跑法
----
    cd D:/wb/urban-flood-gis
    python pipeline/analysis/probe_water.py
"""
import sys
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parents[2]
DEM_DIR = ROOT / "data" / "processed" / "dem"

# (名字, 经度, 纬度, 预期)。预期只是给我自己看的对照，不参与计算。
POINTS = [
    ("东湖·湖心",     114.365, 30.560, "水"),
    ("东湖·听涛",     114.380, 30.555, "水"),
    ("南湖·湖心",     114.335, 30.490, "水"),
    ("沙湖·湖心",     114.318, 30.572, "水"),
    ("汤逊湖·湖心",   114.340, 30.420, "水"),
    ("长江·武汉关",   114.290, 30.580, "水"),
    ("长江·江心",     114.296, 30.577, "水"),
    ("长江·鹦鹉洲",   114.281, 30.556, "水"),
    ("长江·天兴洲",   114.330, 30.610, "水"),
    ("汉江·江心",     114.268, 30.578, "水"),
    ("汉口江滩",      114.300, 30.595, "陆"),
    ("武昌站",        114.315, 30.530, "陆"),
    ("四新·低地",     114.245, 30.510, "陆"),
    ("青山·建一",     114.390, 30.620, "陆"),
]

HALF = 5   # 11x11 邻域，约 330m 见方：单点会撞上湖心小岛或一条船


def main() -> None:
    with rasterio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
        tr = ds.transform
        height, width = ds.height, ds.width

    filled = np.load(DEM_DIR / "wuhan_dem_filled.npy")
    fill = np.load(DEM_DIR / "wuhan_dem_filldepth.npy")
    slope = np.load(DEM_DIR / "wuhan_slope.npy")
    acc = np.load(DEM_DIR / "wuhan_flowacc.npy")
    print(f"网格 {width} x {height}，分辨率 {tr.a:.1f} m\n")

    to_m = Transformer.from_crs("EPSG:4326", "EPSG:4547", always_xy=True)

    def rc(lon, lat):
        x, y = to_m.transform(lon, lat)
        c = int((x - tr.c) / tr.a)
        r = int((y - tr.f) / tr.e)
        return r, c

    def patch(arr, r, c):
        return arr[max(0, r - HALF):r + HALF + 1, max(0, c - HALF):c + HALF + 1]

    print(f"{'点位':<14}{'预期':<4}{'高程m':>8}{'填深m':>8}{'坡度°':>8}{'汇流':>10}")
    print("-" * 56)
    for name, lon, lat, expect in POINTS:
        r, c = rc(lon, lat)
        e = float(np.median(patch(filled, r, c)))
        f = float(np.median(patch(fill, r, c)))
        s = float(np.median(patch(slope, r, c)))
        a = float(np.median(patch(acc, r, c)))
        print(f"{name:<14}{expect:<4}{e:>8.1f}{f:>8.2f}{s:>8.3f}{a:>10,.0f}")

    # ---------------- 全域分布：坡度极小的像元，填深怎么分布 ----------------
    flat = slope < 0.1
    print(f"\n坡度 < 0.1° 的像元：{flat.sum():,}（{flat.mean() * 100:.1f}%）")
    fv = fill[flat]
    qs = [10, 25, 50, 75, 90, 95, 99]
    print("其填深分位(m)：" + "  ".join(
        f"p{q}={np.percentile(fv, q):.2f}" for q in qs))

    for th in (1, 2, 3, 5, 8):
        m = flat & (fill > th)
        print(f"  坡度<0.1 且 填深>{th:>2}m ：{m.sum():>9,} 像元"
              f" = {m.sum() * 30 * 30 / 1e6:>6.1f} km²")

    # ---------------- 现有掩膜 vs 新判据，在东湖上直接比 ----------------
    print("\n东湖区域抽样（114.33~114.42, 30.52~30.60）里，两种判据各抓多少：")
    r0, c0 = rc(114.33, 30.60)
    r1, c1 = rc(114.42, 30.52)
    box = (slice(r0, r1), slice(c0, c1))
    old = (filled[box] <= 22) & (slope[box] < 0.1)
    new = (slope[box] < 0.1) & (fill[box] > 3)
    print(f"  旧判据 elev<=22 & slope<0.1 : {old.sum():>7,} 像元 "
          f"= {old.sum() * 900 / 1e6:.1f} km²")
    print(f"  新判据 slope<0.1 & fill>3   : {new.sum():>7,} 像元 "
          f"= {new.sum() * 900 / 1e6:.1f} km²")
    print(f"  两者都算                     : {(old & new).sum():>7,} 像元")

    # ---------------- 3171 km² 里到底是什么：连通域拆开看 ----------------
    from scipy import ndimage

    print("\n把「坡度<0.1 且 填深>3」按连通域拆开（8 邻域）：")
    lab, n = ndimage.label(new_all := (slope < 0.1) & (fill > 3),
                           structure=np.ones((3, 3), dtype=int))
    sizes = ndimage.sum(new_all, lab, np.arange(1, n + 1))
    km2 = sizes * 900 / 1e6
    print(f"  共 {n:,} 个连通域，总面积 {km2.sum():,.0f} km²")

    # 面积最大的 20 个，看是不是湖
    order = np.argsort(km2)[::-1][:20]
    centers = ndimage.center_of_mass(new_all, lab, (order + 1).tolist())
    to_deg = Transformer.from_crs("EPSG:4547", "EPSG:4326", always_xy=True)
    print(f"\n  {'排名':<5}{'面积km²':>9}   {'中心经纬度':<22}{'高程m':>7}")
    for rank, (idx, (r, c)) in enumerate(zip(order, centers), 1):
        cid = idx + 1
        x = tr.c + (c + 0.5) * tr.a
        y = tr.f + (r + 0.5) * tr.e
        lon, lat = to_deg.transform(x, y)
        e = float(np.median(filled[lab == cid]))
        print(f"  {rank:<5}{km2[idx]:>9.1f}   {lon:.4f}, {lat:.4f}      {e:>6.1f}")

    # ---------------- 面积门槛能砍掉多少 ----------------
    print("\n  只保留面积 >= 门槛的连通域：")
    for th in (0.1, 0.3, 0.5, 1.0, 2.0):
        keep = np.zeros(n + 1, dtype=bool)
        keep[1:] = km2 >= th
        kept = keep[lab]
        print(f"    门槛 {th:>4} km² → {kept.sum() * 900 / 1e6:>8.1f} km²"
              f"（{int(keep.sum()) - 1:>5} 个连通域）")

    # ---------------- 再叠高程上限 ----------------
    print("\n  再加高程上限（湖面都在 26~27m，35m 用来挡山区洼地）：")
    for e_max in (30, 32, 35, 40):
        m = new_all & (filled < e_max)
        print(f"    elev < {e_max}m → {m.sum() * 900 / 1e6:>8.1f} km²")


if __name__ == "__main__":
    main()
