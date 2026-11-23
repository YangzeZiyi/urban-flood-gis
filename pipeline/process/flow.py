#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
flow.py —— M2 第 4 步：汇流累积量（D8 算法）
===============================================================================

这个因子回答一个问题：**这格地上的水，最后汇到哪里去了？**
--------------------------------------------------------------------------
坡度只告诉你"这格斜不斜"，但不知道"上游有多少水经过这里"。
一个 0.5° 的斜坡可能在半山腰（水很快就走了），
也可能在两平方公里汇水区的出口（所有水都从这儿过）——内涝风险天差地别。

汇流累积量就是给每个像元记一个数：**包括它自己在内，上游一共有多少个像元的水流过它**。
数字越大，说明这里是越重要的汇水通道。

D8 算法（Deterministic Eight-node）
--------------------------------------------------------------------------
名字里的 "8" 指 8 个方向。做法分两步：

    第 1 步：定流向
        每个像元看周围 8 个邻居，算"往哪个方向下坡最陡"。
        坡降 = (自己高程 - 邻居高程) / 距离
            正东南西北的距离是 1 个格网（30 m）
            四个对角的距离是 √2 个格网（42.4 m）
        选坡降最大的那个方向，就是水的流向。
        如果所有邻居都比自己高（是个坑），就没有流向 —— 这叫 sink。

    第 2 步：累加
        每个像元初始值 1（自己的水）。
        按**高程从高到低**的顺序遍历，把每个像元的值加到它下游邻居的账上。
        轮到某个像元时，它上游的所有像元一定都已经处理完了 —— 因为上游一定更高。

为什么"顺序"是这个算法最容易错的地方？
--------------------------------------------------------------------------
如果按行、列的自然顺序遍历（就像读文章那样从上到下、从左到右），
轮到下游像元时，它上游的像元可能还没算，累加就漏了。
而且这个错误**不会报错，只是数字偏小**，你看着图还挺正常。

所以必须先 np.argsort 按高程排序，再倒着走。
这一步的代价是 2300 万像元要排一次序，但正确性没有替代方案。

关于"平地"的诚实说明
--------------------------------------------------------------------------
填洼之后，被填的区域是**完全平的**（同一水位）。平地上没有下坡方向，
D8 会判成 sink，汇流在那里断掉。

这不是 bug，物理上是对的：湖面上的水本来就不往一个方向流。
但要知道它的后果 —— **这些平地上的 acc 会停在 1**，
在图上表现为密密麻麻的小点，而不是一条连续的河。
真正的大江大河（长江）因为本身有纵坡降，不会被填平，所以照常计算。

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python process/flow.py               # 全分辨率，约 2~4 分钟
    python process/flow.py --scale 4     # 降采样先看效果

输入：data/processed/dem/wuhan_dem_filled.npy
输出：data/processed/dem/wuhan_flowacc.npy
      data/processed/dem/wuhan_flowdir.npy
      docs/images/wuhan_flowacc.png

===============================================================================
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(line_buffering=True)

PROJECT_DIR = Path(__file__).resolve().parents[2]
FILLED_NPY = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_dem_filled.npy"
OUT_ACC = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_flowacc.npy"
OUT_DIR = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_flowdir.npy"

RES = 30.0

# 8 个方向：(行偏移, 列偏移, 距离倍数)
# 顺序不重要，但下标会被存进 dirs 数组，所以顺序必须和这里一致。
NEIGHBORS8 = (
    (-1, -1, 1.41421356), (-1, 0, 1.0), (-1, 1, 1.41421356),
    (0, -1, 1.0),                      (0, 1, 1.0),
    (1, -1, 1.41421356),  (1, 0, 1.0),  (1, 1, 1.41421356),
)


# ---------------------------------------------------------------------------
# 第 1 步：定流向（D8）
# ---------------------------------------------------------------------------
def d8_dirs(elev, res):
    """算出每个像元流向哪个邻居，返回 (dirs, best_slope)。

    dirs[i, j] 是 NEIGHBORS8 的下标（0~7），-1 表示无流向（sink 或 NaN）。
    best_slope 是那个方向的坡降，用于诊断。

    这里用**向量化**写法：不是逐像元循环，而是对整张图同时算 8 个方向，
    每个方向得到一整张"坡降图"，再取逐像元的最大值。
    2300 万像元 × 8 个方向 = 1.8 亿次减法，numpy 大约 2 秒。
    如果写 Python 双重循环，同样的计算要跑十几分钟。
    """
    h, w = elev.shape

    # pad 一圈，让边缘像元也有 8 个邻居。
    # mode="edge" 复制边缘值，等于假设"地图外面和边缘一样高"，
    # 这样边缘像元不会因为"外面是 0 米"而算出荒谬的坡降。
    p = np.pad(elev, 1, mode="edge")

    best = np.full((h, w), -np.inf, dtype=np.float32)
    dirs = np.full((h, w), -1, dtype=np.int8)

    for k, (dr, dc, dist) in enumerate(NEIGHBORS8):
        # 取"第 k 个方向的邻居"整张平面。
        # 中心窗口是 p[1:1+h, 1:1+w]，邻居窗口就是整体平移 (dr, dc)。
        nb = p[1 + dr:1 + dr + h, 1 + dc:1 + dc + w]

        # 坡降：自己减邻居，除以实际距离（米）。正数表示邻居更低。
        slope = (elev - nb) / (dist * res)

        # 如果这个方向比之前记录的所有方向都陡，就更新方向和坡降。
        # NaN 参与的比较永远为 False，所以无数据区域会保持初始的 -1。
        better = slope > best
        best = np.where(better, slope, best)
        dirs = np.where(better, k, dirs)

    # 坡降 <= 0 说明没有下坡（是坑或者平地），标记为无流向
    dirs[best <= 0] = -1
    return dirs, best


# ---------------------------------------------------------------------------
# 第 2 步：累加
# ---------------------------------------------------------------------------
def accumulate(elev, dirs):
    """按高程从高到低遍历，把每个像元的值加给它的下游邻居。"""
    h, w = elev.shape

    # 每个像元初始 1：代表"自己这一格的水"。
    # 注意单位不是立方米，是"像元个数"。要换成水量，乘以格网面积即可。
    acc = np.ones((h, w), dtype=np.float32)

    # 只对有效像元排序。NaN 参与排序会跑到末尾（升序时），
    # 倒序后就跑到最前面，白白占用循环次数。
    flat_e = elev.ravel()
    valid_idx = np.flatnonzero(~np.isnan(flat_e))

    t0 = time.time()
    # argsort 默认升序（低 -> 高），加 [::-1] 变成降序（高 -> 低）。
    order = valid_idx[np.argsort(flat_e[valid_idx])[::-1]]
    print(f"  排序完成，{len(order):,} 个有效像元，用时 {time.time() - t0:.1f}s")

    flat_acc = acc.ravel()
    flat_dir = dirs.ravel()

    # 展平后的一维下标 idx 和二维坐标 (r, c) 的换算：idx = r * w + c
    # 反过来：r = idx // w，c = idx % w（就是 divmod）
    t0 = time.time()
    n_moved = 0
    for i, idx in enumerate(order):
        d = flat_dir[idx]
        if d < 0:
            continue                       # 无流向，水停在这里

        r, c = divmod(int(idx), w)
        dr, dc, _ = NEIGHBORS8[d]
        nr, nc = r + dr, c + dc

        # 边界检查。pad 只用于算坡降，累加时不能越界写。
        if not (0 <= nr < h and 0 <= nc < w):
            continue

        # ============================================================
        # TODO ★ 把当前像元的水，加到下游邻居的账上 ★
        #
        #   数组名说明：
        #     flat_acc  —— 一维的累积量数组（就是 acc 展平后的视图）
        #     idx       —— 当前像元的一维下标
        #     nr, nc    —— 下游邻居的二维坐标（行、列）
        #     w         —— 图像宽度，用于把二维坐标换成一维下标
        #
        #   要写的形式： flat_acc[下游的一维下标] += flat_acc[当前像元的下标]
        #
        #   下游的一维下标 = nr * w + nc
        #   当前的一维下标 = idx
        #
        #   下面这行是**故意写错的**（什么都没做），改掉它。
        # ============================================================
        flat_acc[nr * w + nc] += flat_acc[idx]

        n_moved += 1

        if (i + 1) % 5_000_000 == 0:
            print(f"    进度 {(i + 1) / len(order) * 100:5.1f}%  "
                  f"用时 {time.time() - t0:5.1f}s")

    print(f"  累加完成，{n_moved:,} 个像元把水送给了下游，"
          f"用时 {time.time() - t0:.1f}s")

    acc[~np.isfinite(elev)] = np.nan
    return acc


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def self_test():
    """一个 5x5 的碗：边界 5 米，第二圈 4 米，中心 3 米。

        所有像元都往低处流，最终 24 个像元的水全部汇到中心那一个格子。
        所以中心的累积量应该是 1 + 24 = 25，是全图最大值。

        如果算出来小于 25，说明累加时漏了上游（顺序错了，或者 TODO 没写对）。
    """
    toy = np.array([
        [5, 5, 5, 5, 5],
        [5, 4, 4, 4, 5],
        [5, 4, 3, 4, 5],
        [5, 4, 4, 4, 5],
        [5, 5, 5, 5, 5],
    ], dtype=np.float32)

    dirs, _ = d8_dirs(toy, RES)
    acc = accumulate(toy, dirs)

    center = acc[2, 2]
    print("  算出的累积量：")
    for row in acc:
        print("    " + " ".join(f"{v:>4.0f}" for v in row))
    print(f"  中心累积量 = {center:.0f}，期望 25（自己 1 + 其余 24 个）")

    ok = abs(center - 25) < 0.5 and np.isclose(acc.max(), 25)
    if ok:
        print("  ✓ 自检通过：24 个像元的水全部汇到中心")
    else:
        print("  ✗ 自检失败。检查 TODO 那一行：是不是漏了 `flat_acc[` 和 `] += `？")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="D8 汇流累积量")
    parser.add_argument("--scale", type=int, default=1, help="降采样倍数")
    parser.add_argument("--skip-test", action="store_true")
    args = parser.parse_args()

    print("=" * 66)
    print("第 0 步：自检")
    print("=" * 66)
    if not args.skip_test and not self_test():
        sys.exit("\n[中止] 自检没过，先别跑 2300 万像元。")

    print()
    if not FILLED_NPY.exists():
        sys.exit(f"[错误] 找不到 {FILLED_NPY}\n       请先跑 process/fill_sinks.py")

    print("=" * 66)
    print("第 1 步：读填洼后的 DEM")
    print("=" * 66)
    elev = np.load(FILLED_NPY)
    if args.scale > 1:
        elev = elev[::args.scale, ::args.scale].copy()
        print(f"  --scale {args.scale}：{elev.shape}，格网 {30 * args.scale} 米")
    else:
        print(f"  形状 {elev.shape}")

    print()
    print("=" * 66)
    print("第 2 步：定流向（D8）")
    print("=" * 66)
    t0 = time.time()
    dirs, best = d8_dirs(elev, RES * args.scale)
    valid = ~np.isnan(elev)
    n_sink = int((dirs[valid] < 0).sum())
    print(f"  用时 {time.time() - t0:.1f}s")
    print(f"  有流向   : {(dirs[valid] >= 0).sum():,} "
          f"({(dirs[valid] >= 0).sum() / valid.sum() * 100:.2f}%)")
    print(f"  无流向   : {n_sink:,} ({n_sink / valid.sum() * 100:.2f}%)"
          f"  <- 填平的平地 + 湖面，水在这里停住")

    # 8 个方向各占多少：如果某个方向占比异常高，可能是边界 pad 出了问题
    print("  流向分布 :")
    names = ["西北", "正北", "东北", "正西", "正东", "西南", "正南", "东南"]
    for k, name in enumerate(names):
        n = int((dirs == k).sum())
        if n:
            print(f"    {name} {n:>9,}  {n / valid.sum() * 100:5.2f}%")

    print()
    print("=" * 66)
    print("第 3 步：累积")
    print("=" * 66)
    acc = accumulate(elev, dirs)

    av = acc[valid]
    print(f"  累积量范围 : {av.min():.0f} ~ {av.max():,.0f} 个像元")
    print(f"  中位 / 均值: {np.median(av):.0f} / {av.mean():.1f}")
    for lo, hi, name in [(1, 2, "孤立（sink）"), (2, 10, "小汇水"),
                         (10, 100, "支沟"), (100, 1000, "支流"),
                         (1000, 1e10, "主干 / 大河")]:
        n = int(((av >= lo) & (av < hi)).sum())
        print(f"    {name:<12} {n:>9,}  {n / av.size * 100:5.2f}%")

    # ---- 出图 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False

        out_png = PROJECT_DIR / "docs" / "images" / "wuhan_flowacc.png"
        out_png.parent.mkdir(parents=True, exist_ok=True)

        fig, axes = plt.subplots(1, 2, figsize=(14, 8))
        fig.suptitle("武汉 D8 汇流累积量（基于填洼后 DEM）", fontsize=14)

        # 累积量跨 6 个数量级（1 到几百万），线性色标会把所有细节压成一个颜色，
        # 必须用 log10。+1 是为了避开 log(0)。
        logacc = np.log10(acc + 1)
        im0 = axes[0].imshow(logacc, cmap="Blues")
        axes[0].set_title("汇流累积量（log10，越深代表上游汇水越多）")
        fig.colorbar(im0, ax=axes[0], shrink=0.7, pad=0.02, label="log10(像元数)")

        # 右图只保留累积量 >= 1000 的像元：这些是真正的汇水通道
        trunk = np.where(acc >= 1000, acc, np.nan)
        im1 = axes[1].imshow(np.log10(trunk + 1), cmap="YlGnBu")
        axes[1].set_title("汇水主干（累积量 ≥ 1000 像元 ≈ 0.9 km²）")
        fig.colorbar(im1, ax=axes[1], shrink=0.7, pad=0.02, label="log10(像元数)")

        for ax in axes:
            ax.set_xticks([])
            ax.set_yticks([])
        fig.tight_layout()
        fig.savefig(out_png, dpi=120, bbox_inches="tight")
        print(f"\n  已出图：{out_png}")
    except Exception as e:
        print(f"\n  [跳过出图] {e}")

    if args.scale == 1:
        np.save(OUT_ACC, acc)
        np.save(OUT_DIR, dirs)
        print(f"  已保存：{OUT_ACC.name}、{OUT_DIR.name}")
    else:
        print("  （降采样模式，不覆盖全分辨率结果）")

    print()
    print("M2 四个因子齐了：高程、填洼深度、坡度、汇流累积量。")
    print("下一步：把四个因子归一化后加权合成易涝指数。")


if __name__ == "__main__":
    main()
