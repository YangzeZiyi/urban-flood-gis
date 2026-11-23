# -*- coding: utf-8 -*-
"""
权重敏感性分析
================================================================================
问题：易涝指数 = 四个因子的加权和，权重是人拍的。拍得不同，结论会变吗？

做法：固定因子、固定归一化，只换权重，比较三套方案的排名一致性
      —— Spearman 秩相关（整体排序像不像）
      —— 前 3% 重合率（"极高"那一档是不是同一批地方）

为什么要做：
    如果换个权重，"极高区"就换了一半，那这套结论是权重决定的，不是数据决定的，
    报告里就必须给出更强的选权理由（专家打分、实测标定）。
    如果排名几乎不变，说明结论由地形本身决定，权重只是微调——这时候才好意思说"稳健"。

运行： python analysis/sensitivity.py
输出： docs/images/wuhan_weight_sensitivity.png
"""
import sys
from pathlib import Path

import numpy as np

THIS = Path(__file__).resolve()
PROJECT_DIR = THIS.parents[2]
sys.path.insert(0, str(PROJECT_DIR / "pipeline" / "process"))

import flood_index as fi          # noqa: E402  复用归一化/合成，保证逻辑一致

DEM_DIR = PROJECT_DIR / "data" / "processed" / "dem"
IMG_DIR = PROJECT_DIR / "docs" / "images"

# 三套方案：你的、参考的地形主导、参考的平权
SCHEMES = {
    "C 汇流主导（当前）": {"elev": 0.20, "slope": 0.15, "fill": 0.15, "acc": 0.50},
    "B 地形主导":         {"elev": 0.30, "slope": 0.25, "fill": 0.15, "acc": 0.30},
    "A 平权":             {"elev": 0.25, "slope": 0.25, "fill": 0.25, "acc": 0.25},
}
TOP_FRAC = 0.03                   # "极高"取前 3%


def load_factors():
    """读四个因子并归一化、定方向。这部分和权重无关，只算一次。"""
    raw = {
        "elev": np.load(DEM_DIR / "wuhan_dem_filled.npy"),
        "fill": np.load(DEM_DIR / "wuhan_dem_filldepth.npy"),
        "slope": np.load(DEM_DIR / "wuhan_slope.npy"),
        "acc": np.load(DEM_DIR / "wuhan_flowacc.npy"),
    }
    factors = {}
    for k in ("elev", "slope", "fill", "acc"):
        u, _, _ = fi.normalize(raw[k], *fi.CLIP[k], use_log=fi.LOG10[k])
        factors[k] = fi.orient(u, fi.SIGN[k])
    return raw, factors


def build_land(raw, shape):
    """陆地掩膜：四因子有效 + 在市域内 + 不是水体。"""
    import rasterio
    valid = (np.isfinite(raw["elev"]) & np.isfinite(raw["slope"])
             & np.isfinite(raw["acc"]))
    with rasterio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
        m_city = fi.city_mask(shape, ds.transform)
    water = (raw["elev"] <= fi.WATER_ELEV_MAX) & (raw["slope"] < fi.WATER_SLOPE_MAX)
    return valid & m_city & ~water


def top_mask(a, land, frac=TOP_FRAC):
    """取陆地范围内指数最高的 frac 部分。"""
    th = np.quantile(a[land], 1 - frac)
    return land & (a >= th)


def spearman(a, b, mask, step=10):
    """手算 Spearman：换成名次后算皮尔逊。抽稀 step 倍只为省时间，不影响精度。"""
    x = a[mask][::step]
    y = b[mask][::step]
    rx = np.empty(x.size, dtype=np.float32)
    ry = np.empty(y.size, dtype=np.float32)
    rx[np.argsort(x, kind="stable")] = np.arange(x.size, dtype=np.float32)
    ry[np.argsort(y, kind="stable")] = np.arange(y.size, dtype=np.float32)
    rx -= rx.mean()
    ry -= ry.mean()
    return float((rx * ry).sum() / np.sqrt((rx * rx).sum() * (ry * ry).sum()))


def main() -> None:
    raw, factors = load_factors()
    land = build_land(raw, factors["elev"].shape)
    km2 = land.sum() * fi.CELL_KM2
    print("=" * 70)
    print(f"陆地样本 {land.sum():,} 像元 = {km2:,.0f} km²")
    print("=" * 70)

    # 复现校验：当前权重算出来必须和已保存的 npy 一模一样
    saved = np.load(DEM_DIR / "wuhan_flood_index.npy")
    cur = fi.combine(factors, SCHEMES["C 汇流主导（当前）"])
    d = np.nanmax(np.abs(saved - cur))
    print(f"复现校验：与 wuhan_flood_index.npy 最大差 {d:.2e}  "
          f"{'✓' if d == 0 else '✗ 逻辑不一致'}")

    idx = {name: fi.combine(factors, w) for name, w in SCHEMES.items()}

    print(f"\n{'方案':<20}{'均值':>8}{'中位':>8}{'p97':>8}{'极高 km²':>11}")
    print("-" * 58)
    for name, a in idx.items():
        v = a[land]
        print(f"{name:<20}{v.mean():>8.3f}{np.median(v):>8.3f}"
              f"{np.quantile(v, 0.97):>8.3f}{top_mask(a, land).sum() * fi.CELL_KM2:>11,.0f}")

    names = list(idx)
    print(f"\n{'两两对比':<34}{'Spearman':>10}{'前3%重合':>10}{'Jaccard':>9}")
    print("-" * 70)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = idx[names[i]], idx[names[j]]
            sa, sb = top_mask(a, land), top_mask(b, land)
            inter = (sa & sb).sum()
            print(f"{names[i][:12]} vs {names[j][:12]:<16}"
                  f"{spearman(a, b, land):>10.4f}"
                  f"{inter / sa.sum() * 100:>9.1f}%"
                  f"{inter / (sa | sb).sum():>9.3f}")

    sa, sb = top_mask(idx[names[0]], land), top_mask(idx[names[1]], land)
    print(f"\n当前方案的极高区（{sa.sum() * fi.CELL_KM2:,.0f} km²）里：")
    print(f"  与 B 方案重合 {(sa & sb).sum() * fi.CELL_KM2:>6,.0f} km²")
    print(f"  只在当前方案　{(sa & ~sb).sum() * fi.CELL_KM2:>6,.0f} km²")

    # ---- 三联图 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False

        s = 4
        fig, axes = plt.subplots(1, 3, figsize=(19, 7), sharey=True)
        for ax, name in zip(axes, names):
            show = idx[name].copy()
            show[~land] = np.nan
            im = ax.imshow(show[::s, ::s], cmap="YlOrRd", vmin=0, vmax=1,
                           interpolation="nearest")
            w = SCHEMES[name]
            ax.set_title(f"{name}\n高程{w['elev']:.2f} 坡度{w['slope']:.2f} "
                         f"填深{w['fill']:.2f} 汇流{w['acc']:.2f}", fontsize=11)
            ax.set_xticks([])
            ax.set_yticks([])
        fig.colorbar(im, ax=axes, fraction=0.015, pad=0.01, label="易涝指数")
        fig.suptitle("权重敏感性：同一份数据，三套权重（红＝相对更危险）", fontsize=15)
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        out = IMG_DIR / "wuhan_weight_sensitivity.png"
        fig.savefig(out, dpi=110, bbox_inches="tight")
        plt.close(fig)
        print(f"\n已出图: {out}")
    except Exception as e:
        print(f"[出图失败] {e}")


if __name__ == "__main__":
    main()
