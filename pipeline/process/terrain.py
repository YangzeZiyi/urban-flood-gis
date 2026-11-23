#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
terrain.py —— M2 第 1 步：读懂 DEM，并修掉它的"先天缺陷"
===============================================================================

M2（地形因子）最终要算出三样东西：

    1. 填洼后的高程   —— 消除假坑，否则水流会断在坑里
    2. 坡度           —— 地面倾斜程度，越平越容易积水
    3. 汇流累积量     —— 每个像元上游有多少水汇过来，越大越容易汇水

判据：高程低 + 坡度小 + 汇流大 = 易涝

今天只做两件事：
    A. 把 DEM 读进内存，打印它的"身份证"，验证坐标换算
    B. 修掉水域的负高程 —— 这是算坡度的前提

为什么 B 不能省？
--------------------------------------------------------------------------
第一次跑这个脚本时，体检报了两处红灯：
    有效像元 98.97%（不到 99%）
    最低高程 -152.5 米（武汉没有低于 0 米的地方）

我一开始以为是重投影引入的误差，于是一路查下去：
    1. 打开重投影前的 wuhan_dem_4326.tif，也有 1162 个负值
       -> 排除重投影，是**源数据**自带的
    2. 把负值转成经纬度，886 个挤在 114.90~114.95°E
       -> 指向武汉东南角的梁子湖一带
    3. 看 5x5 邻域：周边正常水面 12.6 m，它自己是 -32.9 m，落差 33.7 m
    4. 按 300 m 网格聚合：整格 100% 都是负值，是连成片的水域

结论：Copernicus DEM 在部分湖库上记录的是**水下地形**而不是水面高程。
这是该产品的已知特性，不是我们处理错了。

不修的后果（这才是关键）：
    - 湖岸会出现 80° 的假陡崖，坡度图上多一圈假高值
    - 填洼算法会把这片 -152 m 的"深坑"当成巨型洼地，
      周围几十平方公里的水全部灌进来，汇流量算得离谱
    - 最后"易涝点"全跑到湖中心去了

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python process/terrain.py

===============================================================================
"""

import sys
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

# 让 print 立刻显示。不加这行，输出会被缓冲，屏幕上看着像卡住。
sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
# __file__ 是当前文件的路径。.parents[0] 是 process/，[1] 是 pipeline/，
# [2] 就是项目根目录 D:\wb\urban-flood-gis\。
# 用相对路径推算而不是写死 D:\...，是为了别人 clone 仓库后也能直接跑。
PROJECT_DIR = Path(__file__).resolve().parents[2]
DEM_PATH = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_dem_cgcs2000.tif"


# ---------------------------------------------------------------------------
# 工具：地理坐标 <-> 数组行列号
# ---------------------------------------------------------------------------
def xy_to_rowcol(transform, x, y):
    """给一个地图坐标（东坐标 x、北坐标 y），返回它在数组里的 (行, 列)。

    rasterio 的 transform 是个 6 参数的仿射变换（Affine），表达的是：

        东坐标 x = a * col + b * row + c
        北坐标 y = d * col + e * row + f

    对正北朝上的格网，b = d = 0，反解就很简单：

        col = (x - c) / a
        row = (y - f) / e

    注意 e 是**负数**（行号往下增，北坐标往下减），
    所以 (y - f) 是负数除以负数，结果正好是正的行号。这不是笔误。

    为什么不直接用 pyproj？
        因为 DEM 已经是投影坐标（EPSG:4547，单位米）了，
        这里只是"米 -> 第几格"，纯线性换算，不需要坐标系转换。
    """
    col = int((x - transform.c) / transform.a)
    row = int((y - transform.f) / transform.e)
    return row, col


# ---------------------------------------------------------------------------
# 核心：修复水域负高程
# ---------------------------------------------------------------------------
def repair_negative(elev, nodata):
    """把负高程的像元，替换成周围有效像元的中值。

    为什么用中值（median）而不是均值（mean）？
        窗口里只要混进一个 -152，均值立刻被拉低几十米；
        中值取排序后中间那个，不受极端值影响。这叫"稳健统计"。

    为什么要多轮、逐轮放大窗口？
        负值连成一片时，**湖心那几十个点周围 5x5 之内全是负值**，
        一个有效邻居都找不到。第一轮只能修好边缘一圈，
        第二轮窗口放大到 7x7，才能往中心推进一步。
        实测：5x5 修掉 663 个，剩下 376 个全在湖心，
        扩到 7x7 / 9x9 才能收干净。
        ——这就是"连片缺陷不能用逐点邻域一次修完"的典型例子。

    为什么窗口不一开始就开到 31x31？
        窗口越大，越容易把岸上的高地拉进湖里，把水面填高。
        先小后大，能用小窗口解决的就绝不用大窗口。

    返回：(修复后的数组, 修复个数, 平均抬升高度)
    """
    fixed = elev.copy()
    h, w = elev.shape

    total_fixed = 0
    total_lift = 0.0

    for win_size in (5, 7, 9, 11, 15, 21, 31):
        bad = (fixed < 0) & (fixed != nodata)
        rows, cols = np.where(bad)
        if len(rows) == 0:
            break

        half = win_size // 2
        round_fixed = 0
        for r, c in zip(rows, cols):
            win = fixed[max(r - half, 0):r + half + 1,
                        max(c - half, 0):c + half + 1]
            ok = win[(win != nodata) & (win >= 0)]
            if ok.size == 0:
                continue
            new_val = float(np.median(ok))
            total_lift += new_val - fixed[r, c]
            fixed[r, c] = new_val
            round_fixed += 1

        total_fixed += round_fixed
        print(f"  窗口 {win_size:>2}x{win_size:<2}: 修复 {round_fixed:>4} 个"
              f"（剩余 {len(rows) - round_fixed} 个）")
        if round_fixed == 0:
            print("  [警告] 窗口已放到 31x31 仍有修不掉的点，请检查数据")
            break

    avg_lift = total_lift / total_fixed if total_fixed else 0.0
    return fixed, total_fixed, avg_lift


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    if not DEM_PATH.exists():
        sys.exit(f"[错误] 找不到 {DEM_PATH}\n       请先跑 fetch/fetch_dem.py")

    # with 保证文件句柄一定关闭。rasterio 打开的文件不关，
    # Windows 上后续想覆盖写会报"文件被占用"。
    with rasterio.open(DEM_PATH) as ds:
        print("=" * 66)
        print("DEM 身份证")
        print("=" * 66)
        print(f"文件      : {DEM_PATH.name}")
        print(f"尺寸      : {ds.width} 列 x {ds.height} 行 "
              f"= {ds.width * ds.height / 1e6:.1f} 百万像元")
        print(f"波段数    : {ds.count}（高程只需要 1 个波段）")
        print(f"数据类型  : {ds.dtypes[0]}")
        print(f"坐标系    : {ds.crs}")
        print(f"分辨率    : {ds.transform.a:.2f} x {abs(ds.transform.e):.2f} 米")
        print(f"左上角    : x={ds.transform.c:,.1f}, y={ds.transform.f:,.1f}")
        print(f"nodata    : {ds.nodata}")

        # ---- 读数据 ----
        # read(1) 读第 1 波段，得到 (行, 列) 的二维数组。
        # 显式转 float32：后面要算减法、除法，整数类型会截断小数。
        elev = ds.read(1).astype(np.float32)
        nodata = ds.nodata

        # ---- 区分三类像元 ----
        # 这一步是整个文件的地基，务必想清楚：
        #   nodata  : 没有数据（重投影后图像边缘的三角形空区）
        #   valid   : 正常陆地高程，可以参与计算
        #   bad     : 数据存在但物理上不合理（水域负值），需要修复
        is_nodata = elev == nodata
        is_bad = (elev < 0) & ~is_nodata

        print()
        print("=" * 66)
        print("三类像元清点（先分类，再处理，别一锅炖）")
        print("=" * 66)
        print(f"nodata（无数据）: {is_nodata.sum():>9,}  {is_nodata.sum() / elev.size * 100:6.2f}%"
              f"   ← 集中在图像边缘，重投影的必然代价")
        print(f"负高程（待修）  : {is_bad.sum():>9,}  {is_bad.sum() / elev.size * 100:6.2f}%"
              f"   ← 湖库水下地形，物理上不合理")
        print(f"正常陆地        : {(~is_nodata & ~is_bad).sum():>9,}"
              f"  {(~is_nodata & ~is_bad).sum() / elev.size * 100:6.2f}%")

        # ---- 修复 ----
        print()
        print("修复水域负高程（负值 -> 周围 5x5 有效像元的中值）")
        elev, n_fixed, avg_lift = repair_negative(elev, nodata)
        print(f"  已修复 {n_fixed:,} 个像元，平均抬升 {avg_lift:.1f} m")

        # 修复后再做一次统计，用 np.nan 表示 nodata：
        # numpy 的所有统计函数（nanmin/nanmean/nanpercentile）都会自动跳过 nan，
        # 不用再额外写掩膜，代码干净很多。
        elev_nan = np.where(is_nodata, np.nan, elev).astype(np.float32)

        print()
        print("=" * 66)
        print("修复后的数值体检")
        print("=" * 66)
        print(f"最小 / 最大: {np.nanmin(elev_nan):.1f} / {np.nanmax(elev_nan):.1f} m")
        print(f"平均 / 中位: {np.nanmean(elev_nan):.1f} / {np.nanmedian(elev_nan):.1f} m")

        # 分位数比 min/max 更能说明"大多数地方长什么样"
        p = np.nanpercentile(elev_nan, [1, 10, 50, 90, 99])
        print(f"分位数    : P1={p[0]:.1f}  P10={p[1]:.1f}  P50={p[2]:.1f}  "
              f"P90={p[3]:.1f}  P99={p[4]:.1f} m")

        # ---- 合理性断言 ----
        # 每个判据都对应 W1 已核对过的事实，不是拍脑袋定的阈值。
        # 注意"有效像元"的阈值是 98% 而不是 99%：
        #   重投影时图像会旋转，四周必然出现三角形空区。
        #   实测 1.03%，且 33% 紧贴边界、最远不超过 48 格，属于正常损耗。
        #   如果哪天掉到 90% 以下，那才是真的有问题。
        print()
        print("合理性检查：")
        checks = [
            ("坐标系是 EPSG:4547", ds.crs.to_epsg() == 4547),
            ("格网约 30 米", 29.5 < ds.transform.a < 30.5),
            ("有效像元 > 98%（边缘损耗属正常）", (~is_nodata).sum() / elev.size > 0.98),
            ("P1 高程 >= 0（负值已清干净）", p[0] >= 0),
            ("最高点 < 900 m（武汉实际 863.6 m）", np.nanmax(elev_nan) < 900),
        ]
        for name, ok in checks:
            print(f"  {'✓' if ok else '✗'} {name}")
        if not all(ok for _, ok in checks):
            sys.exit("\n[中止] 有检查项不通过，先别算坡度，回头查数据")

        # ---- 坐标换算实战 ----
        # 这是一个"可证伪"的检验：如果行列号算错了，取到的高程会明显不对。
        print()
        print("=" * 66)
        print("坐标换算实战（行列号算错，后面全盘皆错）")
        print("=" * 66)

        # 【关键】地标坐标是经纬度（EPSG:4326），DEM 是米制投影（EPSG:4547）。
        # 必须先做投影转换，否则就是把"114.302 度"当成"114.302 米"代入公式。
        # 我第一次跑就踩了这个坑：三个地标算出的行列号完全相同且越界，
        # 原因就是差了这一步。always_xy=True 保证顺序是 (经度, 纬度)。
        tr = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)

        landmarks = [
            ("黄鹤楼", 114.3020, 30.5450, "武昌蛇山，山体 60~80 m"),
            ("江汉路", 114.2890, 30.5820, "汉口江滩，25~35 m"),
            ("东湖听涛", 114.3700, 30.5600, "湖边低地，约 20 m"),
        ]
        for name, lon, lat, expect in landmarks:
            x, y = tr.transform(lon, lat)          # 经纬度 -> 米制平面坐标
            row, col = xy_to_rowcol(ds.transform, x, y)
            # 边界检查：算出的行列必须在范围内。
            # Python 的负索引会让 elev[-5] 静默取到倒数第 5 行，
            # 给出一个"看起来正常"的错误值——这种 bug 最难查。
            if not (0 <= row < ds.height and 0 <= col < ds.width):
                print(f"  {name}: 行列 ({row}, {col}) 越界！")
                continue
            val = elev[row, col]
            print(f"  {name}: {lon}°E {lat}°N -> E {x:,.0f} N {y:,.0f}"
                  f" -> 行 {row} 列 {col} -> {val:.1f} m   [{expect}]")

    # ---- 存一份 npy，供后续算法反复读取 ----
    # 为什么不每次直接读 GeoTIFF？
    #   GeoTIFF 是 deflate 压缩的，每次读都要解压，5000x5000 约 1~2 秒。
    #   填洼算法要反复扫描、反复调试，每次等 2 秒很折磨。
    #   npy 是内存的原始二进制，读一次 0.1 秒。
    # 注意：npy 不含坐标信息，只是纯数字。坐标仍然从 GeoTIFF 读。
    cache = DEM_PATH.with_suffix(".npy")
    np.save(cache, elev_nan)
    print()
    print(f"已缓存数组：{cache.name}  ({cache.stat().st_size / 1048576:.0f} MB)")
    print()
    print("下一步：在同一个文件里加 fill_sinks()，做填洼（Wang & Liu 算法）。")


if __name__ == "__main__":
    main()
