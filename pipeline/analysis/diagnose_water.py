# -*- coding: utf-8 -*-
"""水体判据诊断（按连通域）：把「真湖」和「封闭洼地」放在同一张表上量。

判据本身是按连通域工作的，所以诊断也必须按连通域，
拿单个点去比会受岸边像元干扰（第一版就是这么错的）。
"""
import sys
from pathlib import Path

import numpy as np

PROJECT = Path(r"D:\wb\urban-flood-gis")
sys.path.insert(0, str(PROJECT / "pipeline" / "process"))

DEM_DIR = PROJECT / "data" / "processed" / "dem"

import flood_index as fi  # noqa: E402

# 这三个是模型判成水、但官方清单里是真易涝点的位置（坐标取自 candidates.json）
BAD = [
    ("玉龙路邹家大湾", 114.2075, 30.5310),
    ("竹叶山立交", 114.2896, 30.6182),
    ("南湖路供销学校", 114.3187, 30.5052),
]
# 真湖参考点（取湖心，避开岸线）
LAKES = [
    ("东湖", 114.3720, 30.5560),
    ("沙湖", 114.3307, 30.5747),
    ("汤逊湖", 114.3420, 30.4280),
    ("梁子湖", 114.5300, 30.2500),
    ("涨渡湖", 114.6300, 30.6900),
    ("后官湖", 114.1230, 30.5270),
]
LAND = [
    ("汉口江滩", 114.2980, 30.6000),
    ("武昌站", 114.3230, 30.5320),
    ("四新", 114.2400, 30.5200),
    ("光谷广场", 114.3990, 30.5070),
]


def main():
    import rasterio
    import scipy.ndimage as ndi
    from pyproj import Transformer

    with rasterio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
        transform, crs, w, h = ds.transform, ds.crs, ds.width, ds.height

    elev = np.load(DEM_DIR / "wuhan_dem_cgcs2000.npy", mmap_mode="r")
    slope = np.load(DEM_DIR / "wuhan_slope.npy", mmap_mode="r")
    fill = np.load(DEM_DIR / "wuhan_dem_filldepth.npy", mmap_mode="r")
    acc = np.load(DEM_DIR / "wuhan_flowacc.npy", mmap_mode="r")

    valid_all = np.isfinite(elev) & np.isfinite(slope) & np.isfinite(acc)
    m_city = fi.city_mask((h, w), transform)
    valid = np.asarray(valid_all & m_city)

    print("=" * 100)
    print("第 1 步：按现行判据算候选连通域")
    print("=" * 100)
    cand = (valid
            & (np.asarray(slope) < fi.WATER_SLOPE_MAX)
            & (np.asarray(fill) > fi.WATER_FILL_MIN)
            & (np.asarray(elev) < fi.WATER_ELEV_MAX))
    labels, n = ndi.label(cand, structure=np.ones((3, 3), dtype=int))
    cell_km2 = 900 / 1e6
    areas = np.bincount(labels.ravel(), weights=None)[1:] * cell_km2
    keep = areas >= fi.WATER_MIN_AREA_KM2
    print(f"候选连通域 {n} 个，其中 >= {fi.WATER_MIN_AREA_KM2} km² 的 {int(keep.sum())} 个")
    print(f"现行判据：坡度<{fi.WATER_SLOPE_MAX}° 且 填深>{fi.WATER_FILL_MIN}m "
          f"且 高程<{fi.WATER_ELEV_MAX}m，面积>={fi.WATER_MIN_AREA_KM2} km²")

    # 每个连通域的统计。必须向量化：连通域可能有几千个，
    # 对每个域做一次 `labels == i` 全图布尔运算 = 几千 × 2,300 万次比较，
    # 第一版这么写直接跑死（SIGTERM）。
    # bincount 求均值、ndi.maximum 求最大值都是单遍扫描。
    idx_all = np.arange(1, n + 1)

    def to_f32(arr):
        a = np.asarray(arr, dtype=np.float32)
        return np.where(np.isfinite(a), a, 0.0), np.isfinite(np.asarray(arr, dtype=np.float32))

    def per_label_mean(arr):
        a, ok = to_f32(arr)
        cnt = np.bincount(labels.ravel(), minlength=n + 1)
        s = np.bincount(labels.ravel(), weights=(a * ok).ravel(), minlength=n + 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)

    def per_label_max(arr):
        a, ok = to_f32(arr)
        a = np.where(ok, a, -np.inf)
        out = np.full(n + 1, np.nan)
        vals = ndi.maximum(a, labels, idx_all)
        out[1:] = vals
        out[np.isneginf(out)] = np.nan
        return out

    mean_elev = per_label_mean(elev)
    mean_fill = per_label_mean(fill)
    mean_slope = per_label_mean(slope)
    max_acc = per_label_max(acc)

    to_xy = Transformer.from_crs(4326, crs, always_xy=True)

    def label_at(lon, lat):
        x, y = to_xy.transform(lon, lat)
        col, row = ~transform * (x, y)
        col, row = int(col), int(row)
        if not (0 <= row < h and 0 <= col < w):
            return 0
        # 落点可能在岸边外一格，取 5×5 邻域里出现最多的非零标签
        sub = labels[max(0, row - 2):row + 3, max(0, col - 2):col + 3]
        vals = sub[sub > 0]
        if vals.size == 0:
            return 0
        return int(np.bincount(vals).argmax())

    def report(title, rows, only_kept=True):
        print()
        print("=" * 100)
        print(title)
        print("=" * 100)
        print(f"{'名称':<16}{'连通域编号':>8}{'面积km²':>10}{'均高程m':>11}"
              f"{'均填深m':>11}{'均坡度°':>11}{'域内max汇流':>13}{'是否判为水':>11}")
        for name, lon, lat in rows:
            lab = label_at(lon, lat)
            if lab == 0:
                print(f"{name:<16}{'—':>8}{'不在任何候选域':>20}  ← 现行判据没把它当水")
                continue
            print(f"{name:<16}{lab:>8}{areas[lab-1]:>10.3f}{mean_elev[lab]:>11.2f}"
                  f"{mean_fill[lab]:>11.2f}{mean_slope[lab]:>11.3f}{max_acc[lab]:>13.0f}"
                  f"{'是' if areas[lab-1] >= fi.WATER_MIN_AREA_KM2 else '否(面积不足)':>11}")

    report("【真湖】", LAKES)
    report("【真陆地】", LAND)
    report("【误判点】", BAD)

    print()
    print("=" * 100)
    print("第 2 步：全部候选连通域按面积排序（前 20 名）")
    print("=" * 100)
    print(f"{'序':>3}{'编号':>7}{'面积km²':>10}{'中位高程m':>11}{'中位填深m':>11}"
          f"{'中位坡度°':>11}{'域内max汇流':>13}{'保留':>6}")
    order = np.argsort(-areas)
    for k, i in enumerate(order[:20], 1):
        lab = i + 1
        print(f"{k:>3}{lab:>7}{areas[i]:>10.2f}{mean_elev[lab]:>11.2f}{mean_fill[lab]:>11.2f}"
              f"{mean_slope[lab]:>11.3f}{max_acc[lab]:>13.0f}"
              f"{'✓' if areas[i] >= fi.WATER_MIN_AREA_KM2 else '✗':>6}")

    # 关键对照：被保留的域 vs 被丢掉的域，在 max_acc 上的分布
    kept_acc = max_acc[1:][keep]
    print()
    print("被保留连通域的 max_acc 分布：")
    for q in (0, 1, 5, 10, 25, 50):
        if kept_acc.size:
            print(f"  p{q:<3} = {np.percentile(kept_acc, q):.0f}")
    print(f"  最小 = {kept_acc.min():.0f}   最大 = {kept_acc.max():.0f}   （共 {kept_acc.size} 个）")


if __name__ == "__main__":
    main()
