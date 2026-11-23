#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fill_sinks.py —— M2 第 2 步：填洼（Priority-Flood / Wang & Liu 算法）
===============================================================================

什么是洼地，为什么要填？
--------------------------------------------------------------------------
DEM 里有些像元比四周都低，像一个"碗"。水流到碗底就停了，出不去。

    5  5  5  5  5
    5  9  9  9  5
    5  9  3  9  5     <- 中间这个 3 被 9 围住，是个洼地
    5  9  9  9  5
    5  5  5  5  5

后果（这是水文分析最经典的问题）：
    - 算汇流时，水灌进碗里就断了，碗下游的汇流量全部偏小
    - 碗会被误判成"最易涝的地方"，而它其实只是个数据瑕疵
    - 洼地越深，误差传播得越远

所以几乎所有水文分析软件（ArcGIS 的 Fill、QGIS 的 r.fill.dir、SAGA 的
Fill Sinks）第一步都是填洼。

真实 DEM 里的洼地从哪来？
    1. 雷达测高噪声（DSM 尤其明显，建筑、树冠边缘）
    2. 重采样时的插值误差
    3. 真实的喀斯特溶洞、矿坑（这种不该填，但没法区分）
    4. 我们上一步修湖库负值时留下的微小不平整

算法思路（Priority-Flood，Barnes et al. 2014）
--------------------------------------------------------------------------
想象往地形上慢慢注水，水从边界往内陆漫：

    1. 把所有**边界像元**放进一个优先队列（小顶堆），按高程排序
    2. 每次弹出**当前水位最低**的那个像元
    3. 看它的 8 个邻居：
         - 邻居比水位高  -> 水漫不上去，邻居保持原样，入队
         - 邻居比水位低  -> 邻居被水淹没，填到当前水位，入队
    4. 重复，直到队列空

为什么用"小顶堆"而不是普通队列？
    因为水总是从最低的缺口往外漫。用普通队列（先进先出），
    可能会先处理一个高处的像元，把水"抬"得太高，导致过度填平。
    小顶堆保证永远先处理当前水位最低的点——这就是 Dijkstra 的思想。

复杂度：每个像元入队、出队各一次，O(n log n)。
    武汉 2300 万像元，纯 Python 大约 3~8 分钟。

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python process/fill_sinks.py               # 全分辨率（慢）
    python process/fill_sinks.py --scale 4     # 降采样 4 倍，先看效果（快）

输入：data/processed/dem/wuhan_dem_cgcs2000.npy（上一步生成）
输出：data/processed/dem/wuhan_dem_filled.npy
      data/processed/dem/wuhan_dem_filldepth.npy
      docs/images/wuhan_fill_depth.png

===============================================================================
"""

import argparse
import heapq
import sys
import time
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(line_buffering=True)

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEM_NPY = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_dem_cgcs2000.npy"
OUT_FILLED = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_dem_filled.npy"
OUT_DEPTH = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_dem_filldepth.npy"

# 8 邻居的偏移量。用 8 邻域而不是 4 邻域：
# 4 邻域只走上下左右，水无法沿对角线流动，
# 在 45° 的斜坡上会算出"阶梯状"的假流向。
NEIGHBORS = ((-1, -1), (-1, 0), (-1, 1),
             (0, -1),           (0, 1),
             (1, -1),  (1, 0),  (1, 1))


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------
def fill_sinks(elev):
    """填洼。输入二维高程数组（NaN 表示无数据），返回 (填后高程, 填洼深度)。

    ★★★ 这个函数里有两处 TODO，需要你填。 ★★★
    填之前先把上面的算法思路读两遍，然后看 self_test() 里的例子。
    """
    h, w = elev.shape
    filled = elev.copy()                    # 填洼后的高程，先拷贝一份
    depth = np.zeros((h, w), dtype=np.float32)   # 每个像元被填了多少米
    visited = np.zeros((h, w), dtype=bool)  # 是否已处理过

    # NaN（无数据）不参与计算。把它标成"已访问"，
    # 算法走到这里就会跳过，相当于在数据空洞处有一道墙。
    visited[np.isnan(elev)] = True

    # ---- 第 1 步：边界像元入队，作为"水从外面流进来"的起点 ----
    # 用 Python 的 list + heapq 维护小顶堆。
    # 堆里存 (高程, 行, 列) 三元组：heapq 按元组第一个元素排序，
    # 高程相同时会继续比较行号，这没关系，只影响同高程点的处理顺序。
    heap = []

    def seed(r, c):
        if not visited[r, c]:
            visited[r, c] = True
            heapq.heappush(heap, (float(elev[r, c]), r, c))

    for c in range(w):          # 上下两条边
        seed(0, c)
        seed(h - 1, c)
    for r in range(1, h - 1):   # 左右两条边（去掉已处理的角）
        seed(r, 0)
        seed(r, w - 1)

    print(f"  边界种子 {len(heap):,} 个")

    # ---- 第 2 步：主循环，水从低处往内陆漫 ----
    n_pushed = len(heap)
    next_mark = 2_000_000
    t0 = time.time()
    while heap:
        z, r, c = heapq.heappop(heap)      # z = 当前水位

        for dr, dc in NEIGHBORS:
            nr, nc = r + dr, c + dc

            # 边界检查：邻居必须还在图像内
            if not (0 <= nr < h and 0 <= nc < w):
                continue
            # 已访问检查：处理过的点和 NaN 都跳过
            if visited[nr, nc]:
                continue

            visited[nr, nc] = True

            # ---- 算法核心（已填好） ----
            # 规则：水只能往高了填，不能往低了挖。
            #   邻居原高程 12 m（山包），水位 9 m  -> 淹不到，还是 12 m
            #   邻居原高程  3 m（碗底），水位 9 m  -> 被淹，填到 9 m
            #
            # 把 old_z 提成局部变量是个好习惯（用户自己改出来的）：
            #   1) 少一次 numpy 单元素索引（2300 万 × 8 邻居 = 1.8 亿次）
            #   2) new_z 和 depth 用的是同一个原高程，
            #      将来谁在这两行中间插一句改 elev 的代码，也不会错位
            old_z = float(elev[nr, nc])
            new_z = max(z, old_z)

            # ---- Priority-Flood + ε（关键补丁）----
            # 被填的像元必须**严格高于**当前水位一点点，不能正好等于。
            #
            # 为什么？不加这一步，填出来的是一块完全平坦的面（同一水位）。
            # 后面算 D8 流向时，平地上 8 个邻居全都一样高，
            # 找不到"最陡下坡"，于是每个像元都成了独立的水槽：
            #   实测长江整条断流，33% 的像元无流向，
            #   最大累积量只有 241 个像元（正常应该是几百万）。
            #
            # 1e-4 米 = 0.1 毫米。对高程、坡度、填洼深度的统计影响可忽略，
            # 但足以让 D8 分出上下游，水就能沿着填平的区域往外流。
            #
            # 只抬高"被水淹没"的像元（new_z <= z）。原本就比水位高的山包
            # 绝对不能动，否则等于把山削平了一块。
            if new_z <= z:
                new_z = z + 1e-4

            depth[nr, nc] = new_z - old_z

            filled[nr, nc] = new_z

            # 把邻居以它的新高程入队，下一步从它继续往外扩
            heapq.heappush(heap, (new_z, nr, nc))
            n_pushed += 1

        # 进度提示：每跨过 200 万的整数关口打印一次。
        # 为什么不用 n_pushed % 2_000_000 < 8？
        #   内层一次推 8 个邻居，到 200 万关口时条件会连续满足 8 次；
        #   而且 \r 在输出被重定向到文件/日志时不生效，
        #   结果同一个进度刷了 8 遍屏。改成"只有跨过里程碑才打印"。
        if n_pushed >= next_mark:
            pct = n_pushed / (h * w) * 100
            print(f"    进度 {pct:5.1f}%  "
                  f"用时 {time.time() - t0:5.1f}s  "
                  f"水位 {z:6.1f} m")
            next_mark += 2_000_000

    print(f"\n  主循环完成，共处理 {n_pushed:,} 个像元，"
          f"用时 {time.time() - t0:.1f}s")

    return filled, depth


# ---------------------------------------------------------------------------
# 自检：用 5x5 的玩具数据验证算法，比在 2300 万像元上 debug 快 1000 倍
# ---------------------------------------------------------------------------
def self_test():
    """在小学具上跑一遍，答案可以手算出来。

        toy 的构造：外圈 1 米（平地），第二圈 9 米（一圈山脊），
        正中心 3 米 —— 一个被山围住的坑。

        水从边界 1 米进来，遇到 9 米的山脊，漫不过去（保持 9）。
        中心那个 3 米比山脊低，会被水淹到 9 米，也就是填了 9 - 3 = 6 米。

        预期结果：只有 [2,2] 这一格深度是 6，其余全是 0。
    """
    toy = np.array([
        [1, 1, 1, 1, 1],
        [1, 9, 9, 9, 1],
        [1, 9, 3, 9, 1],
        [1, 9, 9, 9, 1],
        [1, 1, 1, 1, 1],
    ], dtype=np.float32)

    _, depth = fill_sinks(toy)

    expect = np.zeros_like(toy)
    expect[2, 2] = 6.0

    print("  输入：")
    for row in toy:
        print("    " + "  ".join(f"{v:>4.0f}" for v in row))
    print("  算出填洼深度：")
    for row in depth:
        print("    " + "  ".join(f"{v:>4.1f}" for v in row))
    print("  期望填洼深度：")
    for row in expect:
        print("    " + "  ".join(f"{v:>4.1f}" for v in row))

    # 注意 atol=1e-3：加了 ε 之后，被填的像元会高出 0.0001 米，
    # 中心的结果是 6.0001 而不是精确的 6.0。
    # 用默认容差（相对误差 1e-5）会判成失败，所以显式放宽到 1 毫米。
    # 这不是"把标准降低"，而是承认算法本来就有 0.1 毫米的设计偏差。
    ok = np.allclose(depth, expect, atol=1e-3)
    if ok:
        print("  ✓ 自检通过：只把碗底 3 米填到 9 米，山脊和平地都没动")
    else:
        print("  ✗ 自检失败。请对照上面的算法说明检查 TODO 1 / TODO 2")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="填洼：Priority-Flood 算法")
    parser.add_argument("--scale", type=int, default=1,
                        help="降采样倍数，4 表示每 4 个像元取 1 个（先粗看效果）")
    parser.add_argument("--skip-test", action="store_true", help="跳过自检")
    args = parser.parse_args()

    print("=" * 66)
    print("第 0 步：先在小数据上自检（这一步过了才值得跑真数据）")
    print("=" * 66)
    if not args.skip_test:
        if not self_test():
            sys.exit("\n[中止] 自检没过，先别跑 2300 万像元。")
    print()

    if not DEM_NPY.exists():
        sys.exit(f"[错误] 找不到 {DEM_NPY}\n       请先跑 process/terrain.py")

    print("=" * 66)
    print("第 1 步：读取上一步缓存的 DEM")
    print("=" * 66)
    elev = np.load(DEM_NPY)
    print(f"  形状 {elev.shape}  类型 {elev.dtype}")
    print(f"  有效像元 {(~np.isnan(elev)).sum():,}，"
          f"其中 NaN（无数据）{np.isnan(elev).sum():,}")

    if args.scale > 1:
        s = args.scale
        elev = elev[::s, ::s].copy()
        print(f"  --scale {s}：降采样为 {elev.shape}，格网变成 {30 * s} 米")

    print()
    print("=" * 66)
    print("第 2 步：填洼")
    print("=" * 66)
    filled, depth = fill_sinks(elev)

    print()
    print("=" * 66)
    print("第 3 步：结果体检")
    print("=" * 66)
    has_sink = depth > 0.001
    n_sink = int(has_sink.sum())
    print(f"  被填的像元 : {n_sink:,}  占有效像元 {n_sink / (~np.isnan(elev)).sum() * 100:.3f}%")
    if n_sink:
        print(f"  最大填深   : {depth.max():.2f} m")
        print(f"  平均填深   : {depth[has_sink].mean():.2f} m（只统计被填的）")
        print(f"  填深 P50/P90/P99 : "
              f"{np.percentile(depth[has_sink], [50, 90, 99])[0]:.2f} / "
              f"{np.percentile(depth[has_sink], [90, 99])[0]:.2f} / "
              f"{np.percentile(depth[has_sink], 99):.2f} m")
    print(f"  填后高程范围 : {np.nanmin(filled):.1f} ~ {np.nanmax(filled):.1f} m")

    # ---- 幂等性检验：填过的再填一次，应该一点变化都没有 ----
    # 这是算法正确性的强证明。如果还有洼地，说明主循环漏了某些分支。
    print()
    print("  幂等性检验（对填洼结果再填一次，应该零变化）：")
    _, depth2 = fill_sinks(filled)
    residual = int((depth2 > 0.001).sum())
    if residual == 0:
        print("  ✓ 通过：不存在残留洼地")
    else:
        print(f"  ✗ 失败：还剩 {residual:,} 个洼地，算法有 bug")

    # ---- 出图：填洼深度分布 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False

        out_png = PROJECT_DIR / "docs" / "images" / "wuhan_fill_depth.png"
        out_png.parent.mkdir(parents=True, exist_ok=True)

        fig, axes = plt.subplots(1, 2, figsize=(14, 8))
        fig.suptitle(f"武汉 DEM 填洼结果（{30 * args.scale} m 格网）", fontsize=14)

        im0 = axes[0].imshow(filled, cmap="terrain", vmin=0, vmax=300)
        axes[0].set_title("填洼后高程（m）")
        fig.colorbar(im0, ax=axes[0], shrink=0.7, pad=0.02)

        # 填洼深度用对数拉伸：大多数是 0，少数几米，线性拉伸会全白
        show = np.where(depth > 0, depth, np.nan)
        im1 = axes[1].imshow(show, cmap="autumn_r")
        axes[1].set_title("填洼深度（m，仅显示被填的像元）")
        fig.colorbar(im1, ax=axes[1], shrink=0.7, pad=0.02)

        for ax in axes:
            ax.set_xticks([])
            ax.set_yticks([])
        fig.tight_layout()
        fig.savefig(out_png, dpi=120, bbox_inches="tight")
        print(f"\n  已出图：{out_png}")
    except Exception as e:
        print(f"\n  [跳过出图] {e}")

    # ---- 存盘 ----
    if args.scale == 1:
        np.save(OUT_FILLED, filled)
        np.save(OUT_DEPTH, depth)
        print(f"  已保存：{OUT_FILLED.name}、{OUT_DEPTH.name}")
    else:
        print("  （降采样模式，不覆盖全分辨率结果）")

    print()
    print("下一步：算坡度（Horn 算法），再算汇流累积量（D8）。")


if __name__ == "__main__":
    main()
