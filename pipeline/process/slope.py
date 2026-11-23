#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
slope.py —— M2 第 3 步：坡度（Horn 算法）
===============================================================================

坡度是什么，为什么内涝要看它？
--------------------------------------------------------------------------
坡度 = 地面倾斜的角度。坡度越小，水越不容易流走。

    35° 的陡坡：雨水落地就往下冲，几乎不积水
     2° 的缓坡：水流得很慢，但还能流
   0.5° 的平地：水基本不动，全靠蒸发和下渗

武汉城区大部分地方坡度不到 1°——这就是为什么一场大雨就能淹。
判据里"坡度小"这一条，比"高程低"更能抓住内涝的本质：
汉口的地面高程其实有 25 米，不比别处低，但它是平的。

Horn 算法（1981）
--------------------------------------------------------------------------
用每个像元周围的 3x3 窗口，拟合一个平面，平面的倾斜度就是坡度。

    按数字键盘方位编号 9 个邻居：

        z1  z2  z3        西北  正北  东北
        z4  z5  z6        正西  自己  正东
        z7  z8  z9        西南  正南  东南

    东西方向的坡降（每米水平距离上升多少米）：

        dz/dx = [(z3 + 2·z6 + z9) - (z1 + 2·z4 + z7)] / (8 · 格网大小)

        为什么中间那列（z4/z6）权重是 2？
            因为它们离中心更近（水平距离 1 格），
            而四个角离中心更远（斜向距离 √2 格）。
            权重 1:2:1 是标准 Sobel 算子，等价于"距离倒数加权"。

    南北方向同理：

        dz/dy = [(z7 + 2·z8 + z9) - (z1 + 2·z2 + z3)] / (8 · 格网大小)

    坡度 = arctan( √(dz/dx² + dz/dy²) )，再转成角度。

    注意 z7/z8/z9 在**下面**（南边），所以这个式子是"南 - 北"。
    坡度的模长不受正负影响（平方后抵消），但将来算**坡向**时正负就有意义了。

Python 里怎么高效地取 9 个邻居？
--------------------------------------------------------------------------
不要写双重 for 循环逐个像元算——2300 万次循环要跑几十分钟。

标准做法是**数组切片**：先用 np.pad 在四周补一圈，
然后原图就变成了 9 个错位的视图，每个视图就是一个"邻居平面"。
numpy 对整块数组做加减，速度是逐像元的几百倍。

    p  = np.pad(elev, 1, mode="edge")   # 补一圈，避免边缘算不出来
    z1 = p[0:-2, 0:-2]                  # 左上角那个平面
    ...

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python process/slope.py

输入：data/processed/dem/wuhan_dem_filled.npy（上一步填洼的结果）
输出：data/processed/dem/wuhan_slope.npy
      docs/images/wuhan_slope.png

===============================================================================
"""

import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(line_buffering=True)

PROJECT_DIR = Path(__file__).resolve().parents[2]
FILLED_NPY = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_dem_filled.npy"
OUT_NPY = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_slope.npy"

# 格网大小（米）。从 DEM 的 transform 得知是 30 米。
# 这个值参与除法，如果写错，坡度会整体放大或缩小，必须和 DEM 一致。
RES = 30.0


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------
def slope_horn(elev, res):
    """用 Horn 算法算坡度，返回角度（0~90）。

    ★ 这个函数里有一处 TODO，把 dz/dx 和 dz/dy 的两个公式翻译成代码。★
    """
    # 四周补一圈。mode="edge" 表示用最外圈的像元值填充，
    # 相当于假设"地图边缘外面和边缘一样高"。
    # 为什么不用 mode="constant"（补 0）？
    #   补 0 会在图像边缘造出一圈"万丈深渊"，边缘坡度全部变成 90°。
    p = np.pad(elev, 1, mode="edge")

    # ---- 取出 9 个邻居平面 ----
    # 每个变量的形状都和原图一样，第 [i, j] 个元素是原图 (i, j) 周围的某个邻居。
    # 例如 z1[i, j] 是原图 (i, j) 左上角那个像元的高程。
    z1 = p[0:-2, 0:-2]      # 西北
    z2 = p[0:-2, 1:-1]      # 正北
    z3 = p[0:-2, 2:]        # 东北
    z4 = p[1:-1, 0:-2]      # 正西
    z6 = p[1:-1, 2:]        # 正东
    z7 = p[2:, 0:-2]        # 西南
    z8 = p[2:, 1:-1]        # 正南
    z9 = p[2:, 2:]          # 东南
    # 注意没有 z5：那是中心像元自己，公式里用不到。

    # ----------------------------------------------------------------
    # 公式 -> 代码 的翻译对照表
    #
    # 这一步是最容易卡住的地方：文档里写的是**数学记号**，
    # 而 Python 认识的**语法**是另一套。必须做一次翻译，不能直接照抄。
    #
    #     数学记号           Python 代码        为什么
    #     --------------     ---------------    ----------------------------
    #     dz/dx              dzdx               斜杠是除号，变量名里不能有
    #     [ ... ]            ( ... )            方括号在 Python 里是"列表"
    #     ·（中点乘）         *                  乘号
    #     2·z6               2 * z6             数字和变量之间必须写 *
    #     8 · res            (8 * res)          分母要整体括起来
    #
    # 翻译结果就是下面这两行。
    # ----------------------------------------------------------------
    dzdx = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (8 * res)
    dzdy = ((z7 + 2 * z8 + z9) - (z1 + 2 * z2 + z3)) / (8 * res)

    # 坡度的模长：先平方和开方（hypot 就是 sqrt(a²+b²)，但对大数更稳），
    # 再取 arctan 得到弧度，最后转成角度。
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    return slope


# ---------------------------------------------------------------------------
# 自检：一个手算得出答案的斜面
# ---------------------------------------------------------------------------
def self_test():
    """造一个"西高东低、每 30 米降 30 米"的斜面，坡度必然是 45°。

        列高程：270  240  210  180  150  120   90   60   30    0
        格网 30 m，相邻两列差 30 m  ->  水平走 30 m，垂直降 30 m
        坡降 1:1  ->  45°

        如果算出来不是 45°，公式里一定有问题（比如分母忘了乘 res，
        或者权重写成了 1:1:1）。
    """
    cols = np.arange(9, -1, -1) * 30.0        # [270, 240, ..., 0]
    toy = np.tile(cols, (5, 1)).astype(np.float32)

    s = slope_horn(toy, RES)
    mid = s[1:-1, 1:-1]                        # 只看内部，边缘受 padding 影响

    print("  输入（5x10，列方向每格降 30 m）：")
    for row in toy:
        print("    " + " ".join(f"{v:>4.0f}" for v in row))
    print(f"  算出坡度：min={mid.min():.3f}°  max={mid.max():.3f}°  "
          f"均值={mid.mean():.3f}°")
    print(f"  手算期望：45.000°")

    ok = np.allclose(mid, 45.0, atol=0.01)
    if ok:
        print("  ✓ 自检通过：正切值 1:1 的斜面算出 45°")
    else:
        print("  ✗ 自检失败。检查：分母是不是写成了 8（要 8 * res）？"
              "权重是不是 1,2,1？")
    return ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 66)
    print("第 0 步：自检")
    print("=" * 66)
    if not self_test():
        sys.exit("\n[中止] 自检没过，先别跑真实数据。")

    print()
    if not FILLED_NPY.exists():
        sys.exit(
            f"[错误] 找不到 {FILLED_NPY}\n"
            "       请先跑全分辨率的 process/fill_sinks.py 生成填洼结果"
        )

    print("=" * 66)
    print("第 1 步：读填洼后的 DEM")
    print("=" * 66)
    elev = np.load(FILLED_NPY)
    print(f"  形状 {elev.shape}  类型 {elev.dtype}")
    print(f"  高程范围 {np.nanmin(elev):.1f} ~ {np.nanmax(elev):.1f} m")

    print()
    print("=" * 66)
    print("第 2 步：算坡度（Horn，3x3 窗口）")
    print("=" * 66)
    slope = slope_horn(elev, RES)
    valid = ~np.isnan(slope)

    print(f"  有效像元 {valid.sum():,} / {slope.size:,} "
          f"= {valid.sum() / slope.size * 100:.2f}%")
    print(f"  坡度范围 : {slope[valid].min():.3f} ~ {slope[valid].max():.3f}°")
    print(f"  平均/中位 : {slope[valid].mean():.3f} / {np.median(slope[valid]):.3f}°")

    # ---- 坡度分级（参照《水土保持规划通则》+ 城市内涝的实际需要）----
    # 城市内涝最关心的是"几乎不排水"的那一档，所以单独拆出 <1°。
    print()
    print("  坡度分级：")
    classes = [
        (0, 1, "近乎平地（水基本不流）"),
        (1, 2, "平地"),
        (2, 5, "缓坡"),
        (5, 15, "斜坡"),
        (15, 25, "陡坡"),
        (25, 90.1, "急坡/险坡"),
    ]
    total = valid.sum()
    for lo, hi, name in classes:
        n = int(((slope >= lo) & (slope < hi)).sum())
        bar = "█" * max(1, int(n / total * 40))
        print(f"    {name}（{lo:.0f}~{hi:.0f}°）：{n:>10,}  "
              f"占 {n / total * 100:5.2f}%  {bar}")

    # ---- 出图 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False

        out_png = PROJECT_DIR / "docs" / "images" / "wuhan_slope.png"
        out_png.parent.mkdir(parents=True, exist_ok=True)

        fig, axes = plt.subplots(1, 2, figsize=(14, 8))
        fig.suptitle("武汉 30 m 坡度（Horn 算法，基于填洼后 DEM）", fontsize=14)

        # 左：全图坡度，只拉伸到 15°——超过 15° 的地方极少，拉到 90° 会全黑
        im0 = axes[0].imshow(slope, cmap="YlOrBr", vmin=0, vmax=15)
        axes[0].set_title("坡度（°，色标截断在 15°）")
        fig.colorbar(im0, ax=axes[0], shrink=0.7, pad=0.02)

        # 右：只显示 <1° 的近乎平地，这是内涝的高危区
        flat = np.where(slope < 1.0, slope, np.nan)
        im1 = axes[1].imshow(flat, cmap="Blues_r", vmin=0, vmax=1)
        axes[1].set_title("坡度 < 1° 的区域（内涝高危）")
        fig.colorbar(im1, ax=axes[1], shrink=0.7, pad=0.02)

        for ax in axes:
            ax.set_xticks([])
            ax.set_yticks([])
        fig.tight_layout()
        fig.savefig(out_png, dpi=120, bbox_inches="tight")
        print(f"\n  已出图：{out_png}")
    except Exception as e:
        print(f"\n  [跳过出图] {e}")

    np.save(OUT_NPY, slope.astype(np.float32))
    print(f"  已保存：{OUT_NPY.name}")

    print()
    print("下一步：算汇流累积量（D8），然后三个因子合成易涝指数。")


if __name__ == "__main__":
    main()
