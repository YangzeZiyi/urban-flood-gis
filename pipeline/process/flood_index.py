# -*- coding: utf-8 -*-
"""
易涝指数合成（M3）
================================================================================
把四个地形因子合成一个 0~1 的"易涝指数"，值越大越容易积水。

    易涝指数 = w1 * 高程因子 + w2 * 坡度因子 + w3 * 填洼因子 + w4 * 汇流因子

为什么不能直接把四个数加起来？
    - 高程是 20~860 米，坡度是 0~62 度，填深是 0~119 米，汇流是 1~2100 万。
      量纲完全不同，直接相加等于让"汇流"一个因子说了算。
    - 所以第一步必须把每个因子压到 [0, 1]，第二步才是加权求和。

归一化为什么不用普通的 (x-min)/(max-min)？
    汇流的最大值 21,449,470 是个别像元，p98 只有 1,351。
    按最大值归一化，98% 的像元都会落在 0.0001 附近，图上一片死白——
    数据没错，是那把尺子太长了。正确做法是"截断"：
    只用 p25~p99 这一段做 0~1 的映射，两头超出的一律压平。

方向问题（最容易搞反的地方）：
    归一化只回答"这个值在这群数里算大还是小"，不回答"大是不是坏事"。
      - 高程：越低越危险 -> 反向
      - 坡度：越平越危险 -> 反向
      - 填深：越深越危险 -> 正向
      - 汇流：水越多越危险 -> 正向

运行： python process/flood_index.py
输出： data/processed/dem/wuhan_flood_index.npy
       docs/images/wuhan_flood_index.png          六联图（四因子 + 指数 + 分级）
       frontend/public/overlay/wuhan_flood_index.png  Cesium 用的半透明贴图
"""
import sys
import json
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
THIS = Path(__file__).resolve()
PROJECT_DIR = THIS.parents[2]                    # pipeline/process/x.py -> 项目根
DEM_DIR = PROJECT_DIR / "data" / "processed" / "dem"
IMG_DIR = PROJECT_DIR / "docs" / "images"
OVERLAY_DIR = PROJECT_DIR / "frontend" / "public" / "overlay"

ELEV_NPY = DEM_DIR / "wuhan_dem_filled.npy"
FILLDEP_NPY = DEM_DIR / "wuhan_dem_filldepth.npy"    # 注意文件名是 filldepth 不是 fill_depth
SLOPE_NPY = DEM_DIR / "wuhan_slope.npy"
ACC_NPY = DEM_DIR / "wuhan_flowacc.npy"
OUT_NPY = DEM_DIR / "wuhan_flood_index.npy"
OUT_PNG = IMG_DIR / "wuhan_flood_index.png"
OUT_OVERLAY = OVERLAY_DIR / "wuhan_flood_index.png"
ADMIN_JSON = PROJECT_DIR / "data" / "raw" / "admin" / "wuhan_420100_full.json"

RES = 30.0                                       # 像元边长（米，EPSG:4547）
CELL_KM2 = RES * RES / 1e6                       # 单个像元面积 0.0009 km²

# ---------------------------------------------------------------------------
# 因子配置
# ---------------------------------------------------------------------------
# 截断区间（分位数）。选这几个数的理由：
#   elev  (2, 75)：武汉地面最低 18 m、中位 30.6 m、p75 是 53 m。
#                   取到 p75 是为了让"城区可居住高程段"有完整梯度，
#                   再高的山就不该有高程风险了（山地靠坡度和汇流排除）。
#   slope (25, 90)：p25 是 0°（大片完全平地），p90 是 9.5°。
#                   这样 0° 得 1 分、9.5° 以上得 0 分，正好覆盖"能走水"的范围。
#   fill  (50, 98)：一半像元填深为 0（不是洼地），p98 是 8.1 m。
#                   取 p50 当零点，等于"不是碗就没这个风险"。
#   acc   (25, 99)：汇流跨度 7 个数量级，必须先取 log10（见 normalize）。
#                   p99 是 5,654，再往上是长江主干，压平即可。
CLIP = {
    "elev": (2, 75),
    "slope": (25, 90),
    "fill": (50, 98),
    "acc": (25, 99),
}

# 是否先取 log10。只有汇流需要——它的分布是长尾的，log 之后才接近均匀。
LOG10 = {"elev": False, "slope": False, "fill": False, "acc": True}

LABELS = {
    "elev": "高程（越低越危险）",
    "slope": "坡度（越平越危险）",
    "fill": "填洼深度（越深越危险）",
    "acc": "汇流累积（水越多越危险）",
}

# ---------------------------------------------------------------------------
# TODO ★ 1：给四个因子定方向 ★
#
#   +1 表示"数值越大越危险"，-1 表示"数值越小越危险"。
#
#   判断方法：问自己一句"这个数变大，是更容易积水还是更不容易？"
#     高程 变大 -> 站得更高 -> 水更难淹上来 -> ???
#     坡度 变大 -> 水流得更快 -> 不易停留   -> ???
#     填深 变大 -> 这个碗更深 -> 更容易积   -> ???
#     汇流 变大 -> 上游来的水更多           -> ???
#
#   下面四个 ?? 全是占位符，填错了自检会拦住你。
# ---------------------------------------------------------------------------
SIGN = {
    "elev": None,      # <- 改这里（填 +1 或 -1）
    "slope": None,     # <- 改这里
    "fill": None,      # <- 改这里
    "acc": None,       # <- 改这里
}

# ---------------------------------------------------------------------------
# TODO ★ 2：决定四个因子各占多少权重 ★
#
#   规则：四个数加起来必须等于 1.0。脚本会检查，不等于 1 直接中止。
#
#   这里没有标准答案，只有"能不能自圆其说"。几个参考思路：
#     A. 平权     0.25 / 0.25 / 0.25 / 0.25   最省事，但等于承认四个因子一样重要
#     B. 地形主导 0.30 / 0.25 / 0.15 / 0.30   高程和汇流为主，填深只做微调（当前值）
#     C. 汇流主导 0.20 / 0.15 / 0.15 / 0.50   认为"水从哪来"最关键
#
#   想清楚一件事再动手：你调高某个权重，图上的红色就会往那个因子强的地方跑。
#   权重不是"算出来的"，是"选出来的"——这就是后面要做的敏感性分析。
# ---------------------------------------------------------------------------
WEIGHTS = {
    "elev": None,      # <- 填 0~1 之间的小数
    "slope": None,
    "fill": None,
    "acc": None,
}

# ---------------------------------------------------------------------------
# 水体掩膜（临时的近似判据，M4 会换成真实水体数据）
#
#   为什么必须把水体剔出去？
#     任何"地形汇水"算法都会把湖心、江心排在最前面——它们本来就是水的终点。
#     实测：指数 >= 0.83 的像元里，77% 是水面。把它们算进"极高易涝区"，
#     等于用"湖里最湿"来证明"湖边最危险"，是循环论证。
#
#   判据：高程 <= 22 m（接近长江枯水位）且 坡度 < 0.1°（水面是绝对平的）。
#   代价：会把沿江最低那一层平地（真实的易涝区）一起划掉，属于宁可漏不可错。
#         下一步用 ESA WorldCover / OSM 水系替换这个粗糙判据。
# ---------------------------------------------------------------------------
WATER_ELEV_MAX = 22.0
WATER_SLOPE_MAX = 0.1

# ---------------------------------------------------------------------------
# 分级：按"陆地"指数的分位数切，而不是拍几个绝对阈值
#
#   为什么不用固定阈值？
#     第一版用 0.65 当"极高"，算出 5119 km²（占全市 60%）。
#     一个把六成城区标成"极高"的图，等于什么都没说。
#     分位数保证"极高"永远是相对最危险的那一小撮，阈值随数据自适应。
#
#   代价：换一套权重，边界会跟着动，两版结果不能逐像素对比。
#         要发论文的话，得用实测渍水点标定绝对阈值——我们现在没有标定数据。
# ---------------------------------------------------------------------------
LEVEL_Q = [97, 88, 65, 35]          # 极高 / 高 / 中 / 较低 的分位
LEVEL_STYLE = [
    ("低", "#2f81f7"),
    ("较低", "#3fb950"),
    ("中", "#d29922"),
    ("高", "#f0883e"),
    ("极高", "#f85149"),
]


def build_levels(index, mask):
    """用分位数算出五个等级的分界，返回 [(下限, 上限, 名字, 颜色), ...]。

    np.percentile 按传入顺序返回，这里传的是降序 [97,88,65,35]，
    所以解包时也要按降序接，再倒过来拼成从小到大的边界。
    """
    p97, p88, p65, p35 = np.percentile(index[mask], LEVEL_Q)
    edges = [0.0, p35, p65, p88, p97, 1.001]
    return [(edges[i], edges[i + 1], LEVEL_STYLE[i][0], LEVEL_STYLE[i][1])
            for i in range(5)]


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------
def city_mask(shape, transform):
    """把武汉 13 区的边界烧成和 DEM 同尺寸的布尔掩膜。

    为什么必须裁？
        DEM 是 1°x1° 分幅拼出来的矩形，天然包含孝感、黄冈、鄂州的一部分。
        不裁的话，"陆地面积"会把周边地市算进武汉，分级占比、易涝面积全部虚高
        （实测虚高到 17,751 km²，是市域面积的两倍）。

    注意：DataV 的边界可能是 GCJ-02 坐标，和 DEM 的 CGCS2000 有 300~500 m 偏移。
         对面积统计影响 <2%，但对边界附近的像元有影响。做精细分析前要核实。
    """
    import json as _json
    import rasterio                                    # noqa: F401  (确保依赖在)
    from rasterio.features import rasterize
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform
    from pyproj import Transformer

    gj = _json.loads(ADMIN_JSON.read_text(encoding="utf-8"))
    tf = Transformer.from_crs(4326, 4547, always_xy=True).transform
    geoms = [_transform(tf, _shape(f["geometry"]))
             for f in gj["features"] if f.get("geometry")]
    if not geoms:
        raise ValueError(f"{ADMIN_JSON.name} 里没有可用的多边形")

    m = rasterize(geoms, out_shape=shape, transform=transform,
                  fill=0, default_value=1, dtype="uint8")
    return m.astype(bool)


def normalize(x, lo_p, hi_p, use_log=False):
    """把任意量纲的数组压到 [0, 1]，用分位数截断处理长尾。

    步骤：
      1. （可选）取 log10，把 1~2100 万的跨度压成 0~7.3
      2. 算 lo = 第 lo_p 百分位，hi = 第 hi_p 百分位
      3. u = (x - lo) / (hi - lo)，再 clip 到 [0, 1]
         —— 比 lo 低的都算 0，比 hi 高的都算 1

    NaN 保持 NaN（不参与统计，也不参与出图）。
    返回 (归一化数组, lo, hi)，lo/hi 用于打印"这把尺子到底多长"。
    """
    v = np.log10(np.maximum(x, 1.0)) if use_log else x.astype(np.float32, copy=False)
    valid = np.isfinite(v)
    if not valid.any():
        raise ValueError("整张图都是 NaN，检查输入文件")

    lo, hi = np.percentile(v[valid], [lo_p, hi_p])
    if hi <= lo:
        raise ValueError(f"截断区间无效：p{lo_p}={lo} >= p{hi_p}={hi}")

    u = np.clip((v - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)
    u[~valid] = np.nan
    return u, float(lo), float(hi)


def orient(u, sign):
    """按方向翻转。sign=-1 时用 1-u（越低越危险），+1 时原样返回。

    为什么用 1-u 而不是 -u？
        1-u 仍然落在 [0,1]，量纲不变；-u 会变成 [-1,0]，
        后面加权求和时正负相消，结果就没法解释了。
    """
    return (1.0 - u) if sign < 0 else u


def combine(factors, weights):
    """加权求和。factors 是 {名字: [0,1] 数组}，weights 是 {名字: 权重}。

    NaN 处理：任何一个因子是 NaN，结果就是 NaN。
        这里用"先填 0 再累加，最后把无效像元标回 NaN"的写法，
        避免 0 * NaN = NaN 让整张图被一个缺测点污染。
    """
    total = None
    bad = None
    for name, arr in factors.items():
        w = weights[name]
        a = np.where(np.isfinite(arr), arr, 0.0).astype(np.float32)
        term = a * w
        total = term if total is None else total + term
        miss = ~np.isfinite(arr)
        bad = miss if bad is None else (bad | miss)

    total = total.astype(np.float32)
    total[bad] = np.nan
    return total


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def self_test():
    """配置填全了吗？归一化、方向、加权三件事成立吗？"""
    # ---- 0. 先看 TODO 填完了没有 ----
    bad_sign = [k for k, v in SIGN.items() if v not in (-1, 1)]
    if bad_sign:
        print(f"    ✗ SIGN 还有 {len(bad_sign)} 个没填：{bad_sign}")
        print("       +1 = 数值越大越危险，-1 = 数值越小越危险")
        return False
    if any(v is None for v in WEIGHTS.values()):
        print(f"    ✗ WEIGHTS 还有没填的：{[k for k, v in WEIGHTS.items() if v is None]}")
        return False

    ok = True

    # ---- 1. 归一化的边界与截断 ----
    x = np.array([0, 5, 10, 15, 20, 100], dtype=np.float32)
    u, lo, hi = normalize(x, 0, 80)          # p0=0, p80≈20
    checks = [
        ("最小值映射到 0", np.isclose(u[0], 0.0)),
        ("截断值映射到 1", np.isclose(u[4], 1.0)),
        ("超上限被压平", np.isclose(u[5], 1.0)),
        ("中间值单调", u[1] < u[2] < u[3]),
        ("NaN 还是 NaN", np.isnan(normalize(np.array([1.0, 50.0, np.nan]), 0, 100)[0][2])),
    ]
    for msg, cond in checks:
        print(f"    {'✓' if cond else '✗'} {msg}")
        ok &= bool(cond)

    # ---- 2. 四个因子的方向 ----
    # 每组三个值，从左到右是"从安全到危险"。正确答案必须是得分越靠右越高。
    # 注意：这里的期望值是**领域常识**，不是从 SIGN 推出来的——
    #       否则 SIGN 填反了，测试也会跟着反，等于没测。
    cases = {
        "elev":  ("800 m 山头 / 50 m 岗地 / 20 m 江滩", [800.0, 50.0, 20.0]),
        "slope": ("30° 陡坡 / 5° 缓坡 / 0° 平地", [30.0, 5.0, 0.0]),
        "fill":  ("0 m 不积水 / 2 m 浅坑 / 10 m 深坑", [0.0, 2.0, 10.0]),
        "acc":   ("1 个像元 / 100 个 / 10 万个", [1.0, 100.0, 100000.0]),
    }
    for k, (desc, arr) in cases.items():
        u, _, _ = normalize(np.array(arr, dtype=np.float32), 0, 100, use_log=LOG10[k])
        risk = orient(u, SIGN[k])
        cond = bool(risk[0] < risk[1] < risk[2])
        print(f"    {'✓' if cond else '✗'} {k:<6} {desc}  ->  "
              f"{risk[0]:.2f} / {risk[1]:.2f} / {risk[2]:.2f}")
        ok &= cond

    # ---- 3. 权重 ----
    total_w = sum(WEIGHTS.values())
    cond_w = abs(total_w - 1.0) < 1e-6
    print(f"    {'✓' if cond_w else '✗'} 权重合计 = {total_w:.4f}（必须 1.0000）")
    ok &= bool(cond_w)

    # 四个因子全取满分，指数必须正好是 1.0；全取 0，指数必须是 0.0
    ones = {k: np.ones(2, dtype=np.float32) for k in WEIGHTS}
    zeros = {k: np.zeros(2, dtype=np.float32) for k in WEIGHTS}
    cond_max = np.allclose(combine(ones, WEIGHTS), 1.0)
    cond_min = np.allclose(combine(zeros, WEIGHTS), 0.0)
    print(f"    {'✓' if cond_max else '✗'} 四因子全满分 -> 指数 1.0")
    print(f"    {'✓' if cond_min else '✗'} 四因子全零分 -> 指数 0.0")
    ok &= bool(cond_max) and bool(cond_min)

    return bool(ok)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    t_all = time.time()
    print("=" * 74)
    print("第 0 步：自检")
    print("=" * 74)
    if not self_test():
        sys.exit("\n[中止] 自检没过。先看上面哪个 ✗，别急着跑真实数据。")

    print()
    print("=" * 74)
    print("第 1 步：读四个因子")
    print("=" * 74)
    for p in (ELEV_NPY, FILLDEP_NPY, SLOPE_NPY, ACC_NPY):
        if not p.exists():
            sys.exit(f"[错误] 找不到 {p.name}，请先跑完 fill_sinks / slope / flow")
    raw = {
        "elev": np.load(ELEV_NPY),
        "fill": np.load(FILLDEP_NPY),
        "slope": np.load(SLOPE_NPY),
        "acc": np.load(ACC_NPY),
    }
    shape = raw["elev"].shape
    for k, a in raw.items():
        v = a[np.isfinite(a)]
        print(f"  {k:>5}: 形状 {a.shape}  有效 {v.size:>12,}  "
              f"范围 {v.min():>12.2f} ~ {v.max():>15.2f}")

    # 有效掩膜：四个因子都必须是有效值，缺一个就整格作废
    valid_all = (np.isfinite(raw["elev"]) & np.isfinite(raw["slope"])
                 & np.isfinite(raw["acc"]))
    print(f"\n  四因子共同有效: {valid_all.sum():,} / {valid_all.size:,} "
          f"= {valid_all.sum() / valid_all.size * 100:.2f}%  "
          f"（矩形分幅共 {valid_all.sum() * CELL_KM2:,.0f} km²）")

    # 市域裁剪：DEM 是矩形，必须按行政边界裁掉周边地市
    import rasterio as _rio
    with _rio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
        m_city = city_mask(shape, ds.transform)
    valid = valid_all & m_city
    city_km2 = valid.sum() * CELL_KM2
    print(f"  裁剪到市域   : {valid.sum():,} 像元 = {city_km2:,.0f} km²  "
          f"（官方 8,569 km²，差 {abs(city_km2 - 8569) / 8569 * 100:.1f}%）")

    # 水体掩膜：判据和代价见文件顶部 WATER_ELEV_MAX 那段说明
    water = valid & (raw["elev"] <= WATER_ELEV_MAX) & (raw["slope"] < WATER_SLOPE_MAX)
    land = valid & ~water
    print(f"  剔除疑似水体 : {water.sum():,} 像元 = {water.sum() * CELL_KM2:,.0f} km² "
          f"（占市域 {water.sum() / valid.sum() * 100:.1f}%）")
    print(f"  参与评价陆地 : {land.sum():,} 像元 = {land.sum() * CELL_KM2:,.0f} km²")

    print()
    print("=" * 74)
    print("第 2 步：归一化 + 定方向")
    print("=" * 74)
    print(f"  {'因子':<6}{'方向':<6}{'截断区间':<16}{'映射的原始值':<28}{'均值'}")
    print("  " + "-" * 76)
    factors = {}
    for name in ("elev", "slope", "fill", "acc"):
        u, lo, hi = normalize(raw[name], *CLIP[name], use_log=LOG10[name])
        f = orient(u, SIGN[name])
        factors[name] = f
        sign_txt = "越大越危险" if SIGN[name] > 0 else "越小越危险"
        unit = f"log10({lo:.3f}) ~ log10({hi:.3f})" if LOG10[name] else f"{lo:,.3f} ~ {hi:,.3f}"
        print(f"  {name:<6}{sign_txt:<10}p{CLIP[name][0]}~p{CLIP[name][1]:<8}{unit:<30}"
              f"{np.nanmean(f):.3f}")

    print()
    print("=" * 74)
    print("第 3 步：加权合成")
    print("=" * 74)
    for name in ("elev", "slope", "fill", "acc"):
        print(f"  {LABELS[name]:<26} 权重 {WEIGHTS[name]:.2f}")
    index = combine(factors, WEIGHTS)
    v = index[np.isfinite(index)]
    print(f"\n  指数范围 {v.min():.4f} ~ {v.max():.4f}    "
          f"均值 {v.mean():.4f}  中位 {np.median(v):.4f}")

    print()
    print("=" * 74)
    print("第 4 步：分级统计（分位数阈值，只在陆地上算）")
    print("=" * 74)
    levels = build_levels(index, land)
    print(f"  阈值取陆地指数的 p{LEVEL_Q[3]} / p{LEVEL_Q[2]} / p{LEVEL_Q[1]} / p{LEVEL_Q[0]}，"
          f"阈值本身由数据决定：")
    print(f"  {'等级':<6}{'区间':<20}{'像元数':>12}{'面积 km²':>12}{'占比':>9}")
    print("  " + "-" * 62)
    for lo, hi, name, _ in levels:
        m = (index >= lo) & (index < hi) & land
        n = int(m.sum())
        print(f"  {name:<6}{f'{lo:.3f} ~ {hi:.3f}':<20}{n:>12,}"
              f"{n * CELL_KM2:>12,.1f}{n / land.sum() * 100:>8.2f}%")
    nw = int(water.sum())
    print(f"  {'水体':<6}{'不参与评价':<18}{nw:>12,}{nw * CELL_KM2:>12,.1f}"
          f"{nw / valid.sum() * 100:>8.2f}%")

    # ---- 与现实对照：几个公认的武汉渍水点 ----
    # 注意这是用 DSM 算的，高楼会被误判成高地（江汉路那条已经在前面验证过），
    # 所以这里只当"体检"，不当"判决"。
    spots = [
        ("南湖（洪山）", 114.3350, 30.4850),
        ("汤逊湖（江夏）", 114.3800, 30.4300),
        ("后湖（江岸）", 114.3050, 30.6500),
        ("四新（汉阳）", 114.2400, 30.5100),
        ("青山建一", 114.3900, 30.6200),
        ("武昌站", 114.3170, 30.5290),
    ]
    try:
        from pyproj import Transformer
        tf = Transformer.from_crs(4326, 4547, always_xy=True)
        meta = json.loads((DEM_DIR / "wuhan_dem_meta.json").read_text(encoding="utf-8"))
        # 用投影后的左上角反推像元行列：读 tif 的 transform 最稳
        import rasterio
        with rasterio.open(DEM_DIR / "wuhan_dem_cgcs2000.tif") as ds:
            trans, h, w = ds.transform, ds.height, ds.width

        print()
        print("  已知渍水点的指数值（用 DSM 算的，城市中心会被低估）：")
        for name, lon, lat in spots:
            X, Y = tf.transform(lon, lat)
            col, row = ~trans * (X, Y)
            row, col = int(row), int(col)
            if not (0 <= row < h and 0 <= col < w):
                print(f"    {name:<14} 越界")
                continue
            val = index[row, col]
            tag = "水体" if water[row, col] else next(
                (n for lo, hi, n, _ in levels if lo <= val < hi), "?")
            print(f"    {name:<14} 指数 {val:.3f}  -> {tag}")
    except Exception as e:                       # 对照只是锦上添花，失败不影响主流程
        print(f"\n  [跳过地标对照] {e}")

    # ---- 存盘 ----
    np.save(OUT_NPY, index)
    print(f"\n  已保存: {OUT_NPY.name}  ({OUT_NPY.stat().st_size / 1e6:.0f} MB)")

    # ---- 出图 ----
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap, BoundaryNorm
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False

        IMG_DIR.mkdir(parents=True, exist_ok=True)
        s = 4                                    # 抽稀 4 倍出图，全分辨率没必要
        fig, axes = plt.subplots(2, 3, figsize=(19, 12))
        ext = [0, shape[1] * RES / 1000, shape[0] * RES / 1000, 0]   # km

        panels = [
            ("elev", "① 高程因子", "YlGnBu_r"),
            ("slope", "② 坡度因子", "YlOrRd"),
            ("fill", "③ 填洼深度因子", "PuBu"),
            ("acc", "④ 汇流因子（log）", "YlGn"),
        ]
        for ax, (key, title, cmap) in zip(axes.flat[:4], panels):
            im = ax.imshow(factors[key][::s, ::s], cmap=cmap, vmin=0, vmax=1,
                           extent=ext, interpolation="nearest")
            ax.set_title(f"{title}  （权重 {WEIGHTS[key]:.2f}）", fontsize=12)
            ax.set_xlabel("东西 (km)")
            ax.set_ylabel("南北 (km)")
            plt.colorbar(im, ax=ax, fraction=0.035, pad=0.02)

        # 合成指数（水体不涂色，留白）
        ax = axes.flat[4]
        show = index.copy()
        show[water | ~valid] = np.nan
        cmap_idx = plt.cm.YlOrRd.copy()
        cmap_idx.set_bad("#cfd8dc")
        im = ax.imshow(show[::s, ::s], cmap=cmap_idx, vmin=0, vmax=1,
                       extent=ext, interpolation="nearest")
        ax.set_title("⑤ 易涝指数（合成，水体留白）", fontsize=13, fontweight="bold")
        ax.set_xlabel("东西 (km)")
        ax.set_ylabel("南北 (km)")
        plt.colorbar(im, ax=ax, fraction=0.035, pad=0.02)

        # 分级：0 号色是水体，1~5 号是五档
        ax = axes.flat[5]
        disp = index.copy()
        disp[water] = -1.5                    # 塞一个不会出现的值当"水体"标记
        disp[~valid] = np.nan
        edges = [-2.0, -1.0] + [l[0] for l in levels[1:]] + [1.001]
        cmap6 = ListedColormap(["#aeb8c2"] + [c for _, _, _, c in levels])
        ax.imshow(disp[::s, ::s], cmap=cmap6, norm=BoundaryNorm(edges, 6),
                  extent=ext, interpolation="nearest")
        ax.set_title("⑥ 分级（分位数阈值）", fontsize=13, fontweight="bold")
        ax.set_xlabel("东西 (km)")
        ax.set_ylabel("南北 (km)")
        from matplotlib.patches import Patch
        handles = [Patch(facecolor=c, label=f"{n}  {lo:.2f}~{min(hi, 1.0):.2f}")
                   for lo, hi, n, c in levels]
        handles.append(Patch(facecolor="#aeb8c2", label="水体（不参与评价）"))
        ax.legend(handles=handles, loc="lower left", fontsize=9, framealpha=0.9)

        fig.suptitle("武汉易涝指数合成：四个地形因子的加权叠加", fontsize=16, y=0.995)
        fig.tight_layout()
        fig.savefig(OUT_PNG, dpi=110, bbox_inches="tight")
        plt.close(fig)
        print(f"  已出图: {OUT_PNG}")
    except Exception as e:
        print(f"  [出图失败] {e}")

    # ---- Cesium 贴图 ----
    # 为什么是"半透明贴图"而不是 GeoJSON 多边形？
    #   指数的像元有 2268 万个，转成多边形是几十万个矩形，浏览器一定卡死。
    #   用一张 PNG 铺在地表上，GPU 只画 1 个四边形，代价几乎为零。
    #   透明度跟着指数走：低风险区完全透明，红色区才显出来。
    try:
        from PIL import Image
        OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
        s = 4
        sub = index[::s, ::s]
        v = np.nan_to_num(sub, nan=0.0)
        v[(water | ~valid)[::s, ::s]] = 0.0   # 水体和市域外都不上色

        # YlOrRd 手动插值成 5 段（不依赖 matplotlib 的 colormap 对象）
        stops = np.array([
            [0.00, 255, 255, 204],
            [0.35, 254, 217, 118],
            [0.55, 254, 153, 41],
            [0.75, 227, 26, 28],
            [1.00, 128, 0, 38],
        ], dtype=np.float32)
        rgb = np.zeros(v.shape + (3,), dtype=np.float32)
        for i in range(3):
            rgb[..., i] = np.interp(v, stops[:, 0], stops[:, i + 1])
        rgb = rgb.astype(np.uint8)

        # alpha：从"中"这一档（p65）开始显色，到"极高"（p97）接近全不透明
        lo_a, hi_a = levels[2][0], max(levels[4][0], levels[2][0] + 1e-3)
        alpha = np.clip((v - lo_a) / (hi_a - lo_a), 0.0, 1.0) ** 1.1 * 205
        rgba = np.dstack([rgb, alpha.astype(np.uint8)])
        Image.fromarray(rgba, mode="RGBA").save(OUT_OVERLAY, optimize=True)
        bbox = json.loads((DEM_DIR / "wuhan_dem_meta.json").read_text(encoding="utf-8"))["bbox_wgs84"]
        print(f"  已出贴图: {OUT_OVERLAY.relative_to(PROJECT_DIR)}  "
              f"{rgba.shape[1]}x{rgba.shape[0]}  "
              f"{OUT_OVERLAY.stat().st_size / 1e6:.1f} MB")
        print(f"  贴图范围（西, 南, 东, 北）= {bbox}")
    except Exception as e:
        print(f"  [贴图失败] {e}")

    print()
    print("=" * 74)
    print(f"M3 完成，总用时 {time.time() - t_all:.1f}s")
    print("下一步：把这个指数叠到 Cesium 上，按颜色看红色都落在哪里。")
    print("=" * 74)


if __name__ == "__main__":
    main()
