# -*- coding: utf-8 -*-
"""
动态风险栅格（M4）：静态地形 × 降雨强度 = 随时间变化的风险面
================================================================================
这个脚本在整条链路里的位置：

    静态地形指数（M3，已固定）            降雨观测（M1，每站每天一个数）
    wuhan_flood_index.npy                      rain_record 14,610 行
    2,292 万个格子，几年都不变                   5 站 / 2017-2024 日雨量
               \\                              /
                ↓                            ↓
             前期降雨指数 API（递推，土壤有多饱和）
                          ↓
                 IDW 把 5 个站插成整个面
                          ↓
             rain_factor：API(mm) -> 放大系数 [1, FACTOR_MAX]
                          ↓
      combine：动态风险 = 静态地形易涝性 × 降雨放大（0~1）
                          ↓
   每时段一个文件（.bin 等级 + .png 贴图）+ 一份 risk_timeline.json

为什么要有"动态"这一层？
    静态指数回答"哪里天生容易积水"，这个答案几年不变，一张图就够。
    但它答不了"7 月 5 日晚上哪里最危险"。同一场 100 mm 的雨，
    落在三月干透的地面上是"雨大但没灾"；落在已经连下三天、
    土壤饱和的地面上就是 2020 年 7 月 6 日的南湖。
    差别不在雨，在地的前期状态——所以必须再乘一个随时间变的系数。

为什么**不写进 risk_snapshot 表**？
    risk_snapshot 的粒度是 (time_bucket, zone_id)，是给"按行政区汇总告警"
    用的。而这里要出的是逐像元图：2,292 万格 × 7 个时段 = 1.6 亿行，
    光索引就几十 GB，前端每刷一次还要扫表聚合——数据库不干这个。
    正确做法是"栅格进文件、索引进 JSON"：
      - risk_level_YYYYMMDD.bin   uint8 等级，22 MB/帧，后端 seek 一次取一格
      - risk_YYYYMMDD.png         RGBA 贴图，GPU 只画一个四边形
      - risk_timeline.json        所有帧的统计量 + 几何 + 分级阈值
    前端只读 manifest 就能播时间轴，不连数据库。这一层表保持为空是设计，不是遗漏。

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python process/risk_dynamic.py --only-test
    python process/risk_dynamic.py --start 2020-07-04 --end 2020-07-10

依赖：numpy / rasterio / pyproj / psycopg2 / Pillow（都在 requirements.txt）
================================================================================
"""
import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np

# Windows 控制台默认 GBK，遇到 ✓ 和长横线会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 路径：本文件在 pipeline/process/ 下，所以 parents[1] 是 pipeline，[2] 是项目根
# ---------------------------------------------------------------------------
THIS = Path(__file__).resolve()
PIPELINE_DIR = THIS.parents[1]
PROJECT_DIR = THIS.parents[2]
DEM_DIR = PROJECT_DIR / "data" / "processed" / "dem"
OUT_DIR = PROJECT_DIR / "data" / "processed" / "risk"
OUT_DIR.mkdir(parents=True, exist_ok=True)          # 首次跑自动建目录

ENV_FILE = PIPELINE_DIR / ".env"
INDEX_NPY = DEM_DIR / "wuhan_flood_index.npy"      # M3 静态指数（NaN=水体/市域外）
DEM_TIF = DEM_DIR / "wuhan_dem_cgcs2000.tif"       # 只用它的 transform / 尺寸
DEM_META = DEM_DIR / "wuhan_dem_meta.json"         # 只用它的 bbox_wgs84

# 复用邻居的现成件，绝不重写：
#   markov_rain.load_env   读 pipeline/.env（PGxxx 变量，psycopg2 自动认）
#   flood_index.CELL_KM2   单像元 0.0009 km²，和静态图同一把尺子
#   flood_index.build_levels  按分位数切五档，保证两张图的图例长得一样
sys.path.insert(0, str(PIPELINE_DIR))
from analysis.markov_rain import load_env                       # noqa: E402
from process.flood_index import CELL_KM2, build_levels          # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
RES_M = 30.0               # 成果栅格像元边长（米，EPSG:4547），与 CELL_KM2 配套

# 前期降雨指数（Antecedent Precipitation Index）的日衰减系数。
# API_t = 0.9 * API_{t-1} + R_t
# 0.9 的意思是"今天的湿度有九成留到明天"——一天蒸发掉 10%，约 7 天衰减一半
# （0.9^7 ≈ 0.48），也就是"一周前的雨基本不算数了"。
API_DECAY = 0.9

# 预热天数：API 是递推量，第 1 天从 0 起算会低估。
# 取 40 天，残留 0.9^40 ≈ 1.5%，可以忽略。所以 main 会往前多读 40 天数据。
API_WARMUP_DAYS = 40

# ---------------------------------------------------------------------------
# ★ 待填常量：和下面的 TODO 1 / TODO 2 一起填 ★
#
#   （本节三个数 + 两个函数，正好五个空。check_todo 会一起校验。）
#   单位都是 mm（前期降雨指数，不是当日雨量；所以它的量级是"累积"出来的，
#   连续下一周暴雨能到 1000 以上）。
#
#   API_DRY_MAX —— 低于这个值**完全不放大**，系数 = 1.0。
#       含义："地还是干的，这场雨只是把地浇湿，静态图原样即可"。
#       参考：取 API 序列的 p50 附近（一半的日子都不放大）。
#       填 0 是错的（等于每一滴雨都要放大），所以下面给了 0.0 占位，check_todo 会拦住。
#
#   API_WET_MIN —— 高于这个值**放大到顶**，系数 = FACTOR_MAX。
#       含义："土壤已经泡透，再多下也不改变它已经饱和这件事"。
#       参考：取 API 序列的 p90~p95 附近（只有最湿的那几天顶格）。
#
#   两个门槛怎么找？把这一行临时加到 main() 里 api_series() 之后：
#       print("API 分位", np.percentile(api_all, [50, 90, 95, 99]))
#   打出来的三个数就是"干 / 湿 / 很湿"的分界，抄进来即可（跑完记得删掉这行）。
#   注意 API_WET_MIN 必须 > API_DRY_MAX，否则 rain_factor 的分母是负的。
#
#   FACTOR_MAX —— 最大放大倍数（无量纲，1.0~3.0 之间）。
#       参考：1.5 = 最湿时风险最多涨一半（保守）；
#             2.5 = 最湿时静态指数 0.4 的地方能顶到 1.0（激进，图会很红）。
#       挑它的标准不是"准"（我们没有实测渍水点做标定），是"能不能自圆其说"：
#       它本质是在说"饱和土壤把致涝机会放大了几倍"。这句话你信几倍，就填几倍。
# ---------------------------------------------------------------------------
API_DRY_MAX = 30.0          # <- 改这里（mm）
API_WET_MIN = 500.0         # <- 改这里（mm）
FACTOR_MAX = 2.0            # <- 改这里（倍数）

# 贴图每档的透明度。0 号是"低"：完全透明，否则整张图糊成一块灰。
# 为什么不按风险值连续算 alpha？等级图要能一眼数清"几个档"，连续 alpha 会骗眼睛。
LEVEL_ALPHA = [0, 70, 120, 175, 225]

# 五档分界（lo, hi, 名字, 颜色）。由 ensure_levels() 在 main 里填，
# 全场所有帧共用同一套阈值——否则每天各切各的分位数，
# 每天"极高"都恰好占 3%，时间轴上看不出雨大雨小的区别，等于白做。
LEVELS: list[tuple] = []

_GEOM: dict = {}           # tif 几何缓存，见 geometry()


# ---------------------------------------------------------------------------
# 读数据
# ---------------------------------------------------------------------------
def load_rain(start: str | None = None, end: str | None = None):
    """一次取出 5 个站的日雨量，pivot 成矩阵。

    返回 (dates, codes, mm, lonlat)：
        dates  list[str]      长度 D，升序
        codes  list[str]      长度 S=5，按站号排序（WH01..WH05）
        mm     np.ndarray     [D, S] float64，单位 mm
        lonlat np.ndarray     [S, 2] float64，(经度, 纬度)，WGS84

    pivot 的意思是"长表转宽表"：数据库里一行是"某站某天"，
    我们要的是行=日期、列=站点，这样整条时间轴才能一次算完。

    必须 ORDER BY obs_time：API 是逐日递推的，顺序错一天，后面全错，
    而且错得很隐蔽（图还是能画出来，只是每一帧都对不上真实日期）。
    """
    import psycopg2

    sql = """
        SELECT r.obs_time::date AS d, s.code, r.rainfall_mm
          FROM rain_record r
          JOIN rain_station s ON s.id = r.station_id
         WHERE (%(start)s::date IS NULL OR r.obs_time >= %(start)s::date)
           AND (%(end)s::date   IS NULL OR r.obs_time <= %(end)s::date)
         ORDER BY r.obs_time, s.code
    """
    with psycopg2.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"start": start, "end": end})
            rows = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute("""
                SELECT code, ST_X(geom), ST_Y(geom)
                  FROM rain_station
                 ORDER BY code
            """)
            stations = cur.fetchall()

    if not rows:
        sys.exit(f"[错误] {start} ~ {end} 没有雨量数据，先跑 fetch/fetch_rain.py")

    codes = [s[0] for s in stations]
    lonlat = np.array([[float(s[1]), float(s[2])] for s in stations], dtype=np.float64)
    col_of = {c: i for i, c in enumerate(codes)}

    dates: list[str] = []
    seen: set[str] = set()
    for d, _code, _mm in rows:
        k = str(d)
        if k not in seen:                 # rows 已按日期排序，这里只是去重
            seen.add(k)
            dates.append(k)

    # 缺失的 (日期, 站点) 组合填 0：当作那天没下雨。
    # 为什么是 0 不是 NaN？NaN 会顺着 API 递推污染后面所有天；
    # 0 只会让那一天偏低，误差随时间衰减（0.9^n）。
    # 本数据集 5 站 8 年连续无缺，所以这条只是兜底。
    mm = np.zeros((len(dates), len(codes)), dtype=np.float64)
    row_of = {d: i for i, d in enumerate(dates)}
    for d, code, val in rows:
        mm[row_of[str(d)], col_of[code]] = float(val or 0.0)

    return dates, codes, mm, lonlat


def api_series(mm: np.ndarray, decay: float = API_DECAY) -> np.ndarray:
    """前期降雨指数：API_t = decay * API_{t-1} + R_t，逐站独立递推。

    物理含义：连续降雨会让土壤含水率累积，而土壤的"蓄水能力"是有限的。
    同样 50 mm 落在干地上只是把地浇湿（下渗得掉），
    落在已经泡了三天的地上就全是地表径流，直接进地下车库。
    API 就是给"地已经多湿"记一个会遗忘的账：新雨全额记账，旧账每天打九折。

    这就是为什么 2020-07-05 的 124 mm 和 2020-07-03 的 16 mm
    在静态图上是同一个格子，在动态风险里却是两个世界。

    输入 [D, S]，输出 [D, S] float32。逐站循环（5 次），逐日循环（D 次）——
    递推有先后依赖，这里没法向量化，但 D 只有几千，5×3000 次标量运算毫无压力。
    """
    mm = np.asarray(mm, dtype=np.float64)
    if mm.ndim == 1:                       # 允许传单个站的序列，自检里就这么用
        mm = mm[:, None]
    out = np.zeros_like(mm)
    for s in range(mm.shape[1]):
        acc = 0.0
        for t in range(mm.shape[0]):
            acc = decay * acc + mm[t, s]
            out[t, s] = acc
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# 空间插值
# ---------------------------------------------------------------------------
def station_xy(lonlat: np.ndarray, transform) -> np.ndarray:
    """5 个站的经纬度 -> 栅格 (列, 行) 浮点坐标（像元中心为 .5 偏移的连续坐标）。

    两步：
      1. WGS84 经纬度 -> EPSG:4547 平面米（pyproj，always_xy=True 保证是 (经,纬) 顺序；
         少了这个参数 pyproj 会按 (纬,经) 解释，结果是坐标跑到南极去）
      2. 平面米 -> 栅格列行（rasterio 的 transform 逆矩阵，一次线性变换，没有三角函数）

    为什么必须先投影？IDW 的权重是 1/d^2，d 必须是真实的米。
    在经纬度上算距离会把 1° 经度（约 96 km）和 1° 纬度（约 111 km）当成一样长，
    东西方向的距离被系统性低估 14%，插值面会朝东西方向拉长。
    """
    from pyproj import Transformer

    tf = Transformer.from_crs(4326, 4547, always_xy=True)
    xs, ys = tf.transform(lonlat[:, 0], lonlat[:, 1])
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)

    inv = ~transform                        # 仿射逆矩阵，把 (X, Y) 反解成 (col, row)
    cols = inv.a * xs + inv.b * ys + inv.c
    rows = inv.d * xs + inv.e * ys + inv.f
    return np.column_stack([cols, rows])


def idw_field(values: np.ndarray, sxy: np.ndarray, shape: tuple[int, int],
              power: float = 2.0, eps: float = 1e-6) -> np.ndarray:
    """反距离权重插值：5 个站的值 -> 整幅面。返回 float32[height, width]。

    公式：z(p) = Σ(w_i * z_i) / Σ(w_i)，  w_i = 1 / d(p, i)^power
    站点处 d→0，权重→∞，所以站点上的插值结果严格等于观测值（这叫"精确插值"）。

    d 用米：像素坐标差乘 RES_M。其实这个常数在 w_i / Σw 里会整体约掉，
    换成"像素"结果一模一样；乘上来只是为了让注释和公式对得上，
    也让 eps（最小距离，防止 d=0 除零）有一个真实的物理量纲（米²）。

    IDW 的缺点必须说清楚：站点稀疏时会出现"牛眼"——每个站周围一圈同心圆，
    因为权重只看距离，不看地理（山脊、江面该不该连在一起它不知道）。
    为什么这里还是够用？5 个站覆盖全市 8,569 km²，站间距 20~40 km，
    而日雨量的空间相关性本身就在几十公里尺度上很强（一场雨基本盖住半个市），
    所以真正需要插值解决的问题（"我这格离哪个站更近"）它答得对。
    真想做得更好得上雷达定量降水估计（QPE）或 Kriging，那是另一个项目的量级。

    内存：为什么不先 stack 成 [5, H, W]？本格网 4440×5162 = 2,292 万格，
    [5, H, W] float32 要 458 MB，而按行分块累积只需要 2 个块大小的数组。
    分块大小 BLOCK_ROWS=256 -> 每块 1.1 M 格 -> 每个临时数组 9 MB。
    """
    h, w = shape
    vals = np.asarray(values, dtype=np.float64).ravel()
    xy = np.asarray(sxy, dtype=np.float64)
    if xy.shape[0] != vals.size:
        raise ValueError(f"站值与坐标数量不匹配：{vals.size} vs {xy.shape[0]}")

    out = np.empty((h, w), dtype=np.float32)
    block = 256
    cols = np.arange(w, dtype=np.float64)[None, :] * RES_M      # [1, W] 米
    for r0 in range(0, h, block):
        r1 = min(r0 + block, h)
        rows = np.arange(r0, r1, dtype=np.float64)[:, None] * RES_M   # [B, 1] 米
        num = np.zeros((r1 - r0, w), dtype=np.float64)
        den = np.zeros((r1 - r0, w), dtype=np.float64)
        for i in range(vals.size):
            dx = cols - xy[i, 0] * RES_M
            dy = rows - xy[i, 1] * RES_M
            d2 = dx * dx + dy * dy                              # [B, W] 米²
            wgt = 1.0 / np.maximum(d2, eps) ** (power / 2.0)     # d^-power
            num += wgt * vals[i]
            den += wgt                                          # 广播成 [B, W]
        out[r0:r1] = (num / den).astype(np.float32)
    return out


# ---------------------------------------------------------------------------
# ★ TODO 1：降雨强度 -> 放大系数 ★
# ---------------------------------------------------------------------------
def rain_factor(api: np.ndarray) -> np.ndarray:
    """把前期降雨指数（mm）映射成 [1.0, FACTOR_MAX] 的放大系数。

    要求：
      1. api <= API_DRY_MAX   ->  1.0          （地是干的，不放大，保持静态图原样）
      2. api >= API_WET_MIN   ->  FACTOR_MAX   （地已饱和，放大到顶）
      3. 中间单调过渡，而且必须是**直线**——我们没有任何实测渍水点去标定一条曲线，
         直线是唯一不需要额外假设的选择
      4. 返回 float32，形状与输入相同
      5. 全程不能出现 NaN；api 里万一有 NaN，也要给出 1.0 而不是把 NaN 传下去

    提示：整体思路和 flood_index.normalize 是同一套——先把输入换算成"以门槛为界的
          相对位置"，再线性拉伸到目标区间。

    为什么系数不可能 < 1？降雨只会让风险变大，不会"变好"。
    系数 = 1 的那一档必须严格等于静态图，否则"没下雨的一天"和 M3 的成果对不上，
    用户一对比就以为哪里算错了。
    """
    # ============================================================
    # 实现：三行，正好对应上面三个问题
    # ============================================================

    # 第一步：把 api 换算成"夹在两个门槛之间的相对位置" t。
    #   api = DRY_MAX 时 t = 0，api = WET_MIN 时 t = 1。
    #   分母 (WET_MIN - DRY_MAX) 是区间宽度 —— 它决定了"每多下 1 mm 雨，
    #   风险前进多少"。取 500 和 30 时，这个兑换率是 1/470。
    t = (api - API_DRY_MAX) / (API_WET_MIN - API_DRY_MAX)

    # 第二步：拦住区间外的值。这是最容易漏、后果最直观的一步。
    #   api < DRY_MAX → t 为负；api > WET_MIN → t > 1。
    #   不拦会怎样：
    #     · 大旱天 t = -0.06，系数变成 0.88 —— "不下雨反而更安全"，
    #       等于让静态图的地形风险凭空消失了 12%；
    #     · 特大暴雨 t = 1.4，系数冲到 2.4 —— 超出 FACTOR_MAX 的设定，
    #       图上一片纯红，看不出哪里更危险。
    #   np.clip 一次处理两端，比写两个 np.where 干净。
    t = np.clip(t, 0.0, 1.0)

    # 第三步：把 t 从 [0,1] 线性拉伸到 [1, FACTOR_MAX]。
    #   这是"两点连线"：t=0 → 1.0（不放大），t=1 → FACTOR_MAX。
    #   写成 1 + t*(FACTOR_MAX-1) 而不是 t*FACTOR_MAX：
    #   后者在 t=0 时给 0，把静态指数整个乘没了；前者保证"雨最小的一天
    #   严格等于 M3 的静态图"，两张图能直接叠着对比。
    factor = 1.0 + t * (FACTOR_MAX - 1.0)

    # 第四步：NaN 兜底。
    #   api 里只要有一个 NaN，上面三步会一路把 NaN 传下去，
    #   main 里那句 np.isfinite(factor).all() 会直接报错退出。
    #   给 1.0 的含义是"这一格不知道下没下雨，那就按地形算"——
    #   保守、可解释，且不会让 NaN 污染后面的统计。
    factor = np.where(np.isfinite(factor), factor, 1.0)

    return factor.astype(np.float32)


# ---------------------------------------------------------------------------
# ★ TODO 2：地形 × 降雨，两个方案选一个 ★
# ---------------------------------------------------------------------------
def combine(index_base: np.ndarray, factor: np.ndarray) -> np.ndarray:
    """把静态易涝指数和降雨放大系数合成为动态风险，值域 [0, 1]。

    下面两个方案都在数学上说得通，选一个（或写你自己的），并把理由写进注释。

    ---- 甲：乘性（最简单） ----
        risk = min(index × factor, 1)
      优点：一行，可解释——"风险 = 天生易涝 × 雨有多大"。
      缺点：index_base 高的格子乘 2 直接撞到 1.0 被削平。
            武汉陆地指数 p97 已到 0.69，一场大雨下来最危险的那 3% 全是 1.0000，
            彼此之间的差别（哪个更危险）被吃掉了，图上是一块纯色。
            换句话说：它擅长回答"哪里危险"，不擅长回答"哪里最危险"。

    ---- 乙：剩余风险激活（推荐先试这个） ----
        risk = 1 - (1 - index)^factor
      优点 1：factor = 1 时严格等于 index_base（自己推一遍为什么刚好还原），基准一致。
      优点 2：指数 < 1 时永远撞不到顶，高值区仍然保留排序。
      优点 3：有概率解释。把 (1 - index_base) 看成"这一格这次不积水的概率"，
              factor 就是"这场雨提供了几次独立致涝机会"，
              那么最后得到的量就是"至少淹一次"的概率——和马尔科夫链那套
              "多次机会的补概率"是同一个思路（markov_rain.report 里算
              "7 天内至少一次暴雨"用的也是 1 - 连乘）。
      缺点：公式不如乘法直观，向非技术同事解释要多花两句话。

    注意：无论选哪个，都必须让 factor = 1 时结果等于 index_base；
    也必须保持 NaN 是 NaN（水体/市域外不参与，别被填成 0）。

    上面是数学写法。落到 numpy 要自己翻译：「min(·, 1)」对应哪个函数、
    「^」在 Python 里是 **（星号两遍）不是 ^ —— ^ 是位异或，写下去不报错，
    但结果全错，而且错得很像真的。
    """
    # ============================================================
    # 选乙：risk = 1 - (1 - index) ^ factor
    # ============================================================
    # 拆成三步看，每一步都有物理含义：
    #   1) 1 - index_base  = "还剩多少风险空间没被占"。
    #      指数 0.6 的格子，还剩 0.4。也可以读成"这一格这次不积水的概率"，
    #      两种说法在数学上完全等价，答辩时挑一个讲得顺的。
    #   2) (...) ** factor  = 用这场雨去"压缩"这块剩余空间。
    #      factor=2 → 0.4 变 0.16；factor=1 → 0.4 还是 0.4（原样不动）。
    #      这就是"基准严格还原"的来源：任何数的 1 次方都是它自己。
    #   3) 1 - (...)        = 减回来，得到新风险：0.6 → 0.84。
    #
    # 为什么永远不会撞顶（这是选乙而不是甲的唯一理由）：
    #   剩余空间 (1-index) 只要 > 0，正数无论多少次方都 > 0，
    #   所以 1 减去一个正数，结果必然 < 1。
    #   极端情况 index=0.999、factor=2：0.001 → 0.000001，风险 0.999999。
    #   排序完整保留，"哪里最危险"仍然答得出。
    #   对照甲：index*factor 在 0.6*2=1.2 处被 clip 回 1.0，
    #   所有 >= 0.5 的格子全变成 1.0000。实测 2020-07-07 那种天气下，
    #   甲会把 1,293 km²（陆地的 18.3%）压成同一个数，图上是一块纯色。
    #
    # NaN 不用特意处理：
    #   1 - NaN = NaN，NaN 的任意次方还是 NaN，1 - NaN = NaN。
    #   一路自动保持，水体/市域外不会被悄悄填成 0 ——
    #   "没数据"和"低风险"必须能区分，这是整个项目的底线。
    risk = 1.0 - np.power(1.0 - index_base, factor)

    # float64 算完再降回 float32：栅格有 2,300 万格，float32 省一半内存，
    # 而 7 位有效数字对 4 位小数的展示精度绰绰有余。
    return risk.astype(np.float32)


# ---------------------------------------------------------------------------
# 校验与自检
# ---------------------------------------------------------------------------
def check_todo() -> None:
    """三个常量和两个函数填了没有？没填就立刻停下，别让它静默算出错答案。

    为什么单独写一个函数？因为"系数恒等于 1.0 的图"看起来完全正常——
    它和静态图一模一样，没有人会怀疑。错要错得响亮。
    """
    bad = []
    if API_DRY_MAX <= 0.0:
        bad.append("API_DRY_MAX 还是 0.0：这是「干湿分界」，从 API 序列的 p50 附近开始试")
    if API_WET_MIN <= 0.0:
        bad.append("API_WET_MIN 还是 0.0：这是「饱和门槛」，从 API 序列的 p90~p95 附近开始试")
    if FACTOR_MAX <= 0.0:
        bad.append("FACTOR_MAX 还是 0.0：最大放大倍数，参考 1.5~2.5")
    if API_DRY_MAX > 0.0 and API_WET_MIN > 0.0 and API_WET_MIN <= API_DRY_MAX:
        bad.append(f"API_WET_MIN({API_WET_MIN}) 必须大于 API_DRY_MAX({API_DRY_MAX})，"
                   "否则 rain_factor 的斜率是负的，雨越大风险越小")

    probe = np.linspace(0.0, 100.0, 11)
    try:
        rain_factor(probe)
    except NotImplementedError:
        bad.append("rain_factor() 函数体还是 raise NotImplementedError（TODO 1）")
    except Exception as exc:                     # 填了但写错（除零、广播失败…）
        bad.append(f"rain_factor() 填了但跑不通：{exc!r}")

    idx = np.linspace(0.0, 1.0, 11).astype(np.float32)
    try:
        combine(idx, np.full_like(idx, 1.0))
    except NotImplementedError:
        bad.append("combine() 函数体还是 raise NotImplementedError（TODO 2，甲乙都没选）")
    except Exception as exc:
        bad.append(f"combine() 填了但跑不通：{exc!r}")

    if bad:
        print("=" * 78)
        print("先别跑：还有 %d 处没填" % len(bad))
        print("=" * 78)
        for i, msg in enumerate(bad, 1):
            print(f"  {i}. {msg}")
        print()
        print("  填完之后：python process/risk_dynamic.py --only-test")
        sys.exit(1)


def self_test() -> bool:
    """四项检查，能手算的那种。任何一条 ✗ 都说明还没到能跑真实数据的程度。"""
    ok = True

    def check(msg: str, cond: bool) -> None:
        nonlocal ok
        print(f"    {'✓' if cond else '✗'} {msg}")
        ok &= bool(cond)

    # ---- 1. API 递推：10 mm 之后连干两天，0.9 的衰减 ----
    api = api_series(np.array([10.0, 0.0, 0.0]))
    check(f"API 递推 [10,0,0] -> {np.round(api.ravel(), 3).tolist()}（期望 [10, 9, 8.1]）",
          np.allclose(api.ravel(), [10.0, 9.0, 8.1], rtol=1e-6))

    # ---- 2. rain_factor 的边界与单调性 ----
    dry = float(rain_factor(np.array([0.0], dtype=np.float32))[0])
    wet = float(rain_factor(np.array([1e6], dtype=np.float32))[0])
    check(f"api=0 -> 系数 {dry:.3f}（必须 1.000，否则没下雨的图和静态图对不上）",
          abs(dry - 1.0) < 1e-6)
    check(f"api 很大 -> 系数 {wet:.3f}（必须等于 FACTOR_MAX={FACTOR_MAX}）",
          abs(wet - FACTOR_MAX) < 1e-4)

    sweep = np.linspace(0.0, 2000.0, 201).astype(np.float32)
    f_curve = rain_factor(sweep)
    cond_mono = bool(np.all(np.diff(f_curve.astype(np.float64)) >= -1e-6))
    cond_range = bool(f_curve.min() >= 1.0 - 1e-6 and f_curve.max() <= FACTOR_MAX + 1e-4)
    check(f"系数随 API 单调不减（{f_curve.min():.3f} -> {f_curve.max():.3f}，"
          f"区间 [{1.0:.1f}, {FACTOR_MAX:.1f}]）", cond_mono and cond_range)
    check("系数里没有 NaN", bool(np.isfinite(f_curve).all()))

    # ---- 3. factor=1 时 combine 必须原样返回（甲乙两个方案都成立） ----
    base = np.array([0.0, 0.2, 0.5, 0.8, 0.99], dtype=np.float32)
    same = combine(base, np.ones_like(base))
    check(f"factor=1 时 risk == index_base（{np.round(same, 3).tolist()}）",
          np.allclose(same, base, atol=1e-6))

    # ---- 4. 端到端单调性：同一个格子，雨从 0 加到 500 mm，风险只能涨不能跌 ----
    cell = np.full(64, 0.8, dtype=np.float32)
    risks = []
    for a in np.linspace(0.0, 500.0, 51).astype(np.float32):
        risks.append(float(combine(cell, rain_factor(np.array([a], dtype=np.float32)))[0]))
    cond_e2e = all(b >= a - 1e-6 for a, b in zip(risks, risks[1:]))
    check(f"端到端单调：api 0->500，风险 {risks[0]:.3f} -> {risks[-1]:.3f}", cond_e2e)

    if not ok:
        print("\n    ✗ 有检查没过 —— 先修 TODO（rain_factor / combine / 三个常量）再跑真实数据")
    return bool(ok)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def geometry() -> dict:
    """读一次 tif 拿 width / height / transform / bbox，之后缓存复用。

    为什么几何从 tif 读、而不是在文件里写死 1656×1671？
        因为"写死的数字"和"实际的数组形状"迟早会不一致，而不一致的那一天
        numpy 不会报错，只会悄悄把行当成列。让文件自己说话最安全。
    """
    if _GEOM:
        return _GEOM
    import rasterio

    with rasterio.open(DEM_TIF) as ds:
        _GEOM["width"] = ds.width
        _GEOM["height"] = ds.height
        _GEOM["transform"] = ds.transform
        _GEOM["crs"] = str(ds.crs)
    meta = json.loads(DEM_META.read_text(encoding="utf-8"))
    _GEOM["bbox_wgs84"] = [round(float(v), 6) for v in meta["bbox_wgs84"]]
    return _GEOM


def ensure_levels(index_base: np.ndarray, mask: np.ndarray | None = None) -> list[tuple]:
    """按静态指数的有效像元算五档分界，全场共用。

    为什么阈值取自**静态**指数、而不是每天的动态风险？
        如果每天各切各的 p97，那么"极高"建筑面积每天都是固定的 3%，
        雨大那天和雨小那天看起来一样红——时间轴上什么都看不出来。
        用固定阈值 = 把尺子钉死，才能看出"7 月 6 日那天红色真的多了 8 倍"。

    为什么必须传 mask（陆地）而不是直接用 isfinite？
        【踩过的坑】M3 存盘时写的是 `np.save(OUT_NPY, index)`，
        index 在**水面上也有值**（水体只在出图和统计时被排除，没置 NaN）。
        直接拿 isfinite(index) 当"陆地"会把东湖、梁子湖的湖心算进分位数——
        而湖心恰好是填洼因子最高的地方，指数排在最前面，
        p97 会被湖水抬高，整条图例跟着偏移。
        所以分位数必须算在"M3 用来切等级的那块陆地"上，才能和静态图对上。
    """
    global LEVELS
    m = np.isfinite(index_base) if mask is None else (mask & np.isfinite(index_base))
    LEVELS = build_levels(index_base, m)
    return LEVELS


def build_land_mask(index_base: np.ndarray):
    """市域裁剪 + 剔除水体，复刻 M3 的"可评价陆地"。

    为什么这一步不能省？两个都会毁掉整张图：
      1. 不裁市域：DEM 是 1°×1° 的矩形分幅，天然含孝感、黄冈、鄂州。
         贴图铺出去会盖住邻市，统计面积虚高一倍多（实测矩形 20,398 km²，
         武汉市实际 8,569 km²）。
      2. 不剔水体：任何地形汇水算法都把湖心排在最危险的位置，
         一场大雨下来东湖、梁子湖全变深红——图上最红的是水面，
         等于用"湖里最湿"证明"湖边最危险"，是循环论证。

    判据直接 import flood_index 的 water_mask，不重写：
    两张图必须用同一块地、同一套阈值，否则"静态说这里是中风险、
    动态说这里是极高"这种自相矛盾的画面一定会出现。

    返回 (land, water) 两个布尔数组。缺 npy 文件时降级为"只裁市域"并大声提示。
    """
    from process.flood_index import city_mask, water_mask

    geom = geometry()
    m_city = city_mask(index_base.shape, geom["transform"])
    valid = np.isfinite(index_base) & m_city

    try:
        elev = np.load(DEM_DIR / "wuhan_dem_filled.npy")
        slope = np.load(DEM_DIR / "wuhan_slope.npy")
        fill = np.load(DEM_DIR / "wuhan_dem_filldepth.npy")
    except FileNotFoundError as exc:
        print(f"  [提示] 缺 {Path(exc.filename).name}，跳过水体剔除"
              f"（湖面也会被上色，看图时会显得很红）")
        return valid, np.zeros(index_base.shape, dtype=bool)

    water = water_mask(elev, slope, fill, valid)
    del elev, slope, fill          # 274 MB，用完立刻还给系统，别拖到出图那一步
    return valid & ~water, water


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """'#f85149' -> (248, 81, 73)。"""
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def wgs84_geometry() -> tuple[int, int, object]:
    """导出网格的尺寸与仿射变换：EPSG:4326，与静态贴图、点击查询同一个网格。

    为什么单独抽出来？export_snapshot 用它出图，write_manifest 用它写
    width/height。两处各算一遍，迟早有一天只改了一处——那天的图会整体错位
    半个屏幕，而且不报错。这类"两套数字本该相等"的地方，必须只有一个来源。
    """
    from rasterio.transform import from_bounds
    from process.flood_index import CLICK_RES_DEG

    bw, bs, be, bn = geometry()["bbox_wgs84"]
    ow = int(round((be - bw) / CLICK_RES_DEG))
    oh = int(round((bn - bs) / CLICK_RES_DEG))
    return ow, oh, from_bounds(bw, bs, be, bn, ow, oh)


def export_snapshot(day: str, risk: np.ndarray, index_base: np.ndarray,
                    levels: list[tuple]) -> dict:
    """把一个时段的风险面写成 .bin + .png，返回该时段的汇总 dict。

    产物（都在 data/processed/risk/）：
        risk_level_20200706.bin   uint8，0~4 是等级，255 无数据，行优先、无头
        risk_20200706.png         4440×5162 RGBA 贴图，无数据处 alpha=0

    为什么等级另存一份 bin，不直接从 png 反查颜色？
        png 有损（降到 8 位、抗锯齿、浏览器缩放），颜色一改，反查表就失效；
        bin 是无损的整数，后端点击查询 seek 一次就拿到等级，和 png 外观解耦。
    """
    from PIL import Image

    geom = geometry()
    h, w = geom["height"], geom["width"]
    if risk.shape != (h, w):
        raise ValueError(f"风险面形状 {risk.shape} 与栅格 {(h, w)} 不一致")

    valid = np.isfinite(risk) & np.isfinite(index_base)   # 静态无值的地方动态也不必给值
    # 等级：用 levels[1:] 的下限当切点（p35, p65, p88, p97）-> 0..4
    # 这里用「逐个阈值累加布尔」而不是 np.digitize：digitize 会先返回一个
    # int64 的 2,292 万元素中间数组（183 MB），累加只用一个 23 MB 的 uint8。
    edges = [lv[0] for lv in levels[1:]]
    lvl = np.zeros(risk.shape, dtype=np.uint8)
    for edge in edges:
        lvl += (risk >= edge)                           # NaN 比较为 False，落在 0 档
    lvl[~valid] = 255                                   # 255 = 无数据（水体/市域外）

    # ---- 导出前重采样到 EPSG:4326 ----
    # 分工必须清楚：
    #   计算在 4547 的 30 m 网格上做 —— 那里长度是米、面积是平方米，
    #   坡度和 CELL_KM2 才有意义；
    #   导出换成 4326 的等经纬度网格 —— Cesium 贴图、点击查询要的都是它。
    # 两者不能混：4547 的矩形在经纬度下是梯形，直接按 bbox 铺会错 1.5 km
    # （静态贴图踩过，见 flood_index.to_wgs84_grid 的说明）。
    # 这里的目标网格与静态贴图、点击查询的 grid.bin 完全相同，
    # 于是"眼睛看到的颜色"和"手指点到的数值"永远是同一个格子。
    from process.flood_index import to_wgs84_grid

    ow, oh, dst_transform = wgs84_geometry()
    # 255 先换成 NaN：nearest 会忠实地把 255 也当成一个值搬过去，
    # 到了 4326 网格上再想区分"无数据"和"等级 255"就晚了。
    src = np.where(lvl == 255, np.nan, lvl.astype(np.float32))
    out = to_wgs84_grid(src, geom["transform"], geom["crs"], ow, oh, dst_transform)
    # np.rint 不能省：nearest 回来的是 3.0000002 这类浮点数，
    # 直接 astype(np.uint8) 会向零截断成 2，整整掉一档。
    lvl_w = np.where(np.isfinite(out), np.rint(out), 255).astype(np.uint8)

    tag = day.replace("-", "")
    bin_path = OUT_DIR / f"risk_level_{tag}.bin"
    png_path = OUT_DIR / f"risk_{tag}.png"
    lvl_w.tofile(bin_path)

    rgba = np.zeros((oh, ow, 4), dtype=np.uint8)
    for i, (_lo, _hi, _name, color) in enumerate(levels):
        m = lvl_w == i                 # 255 不落在 0~4，天然被排除
        r_c, g_c, b_c = hex_to_rgb(color)
        rgba[m, 0] = r_c
        rgba[m, 1] = g_c
        rgba[m, 2] = b_c
        rgba[m, 3] = LEVEL_ALPHA[i]
    Image.fromarray(rgba, mode="RGBA").save(png_path, optimize=True)

    # ---- 统计（只在有效像元上算，NaN 不参与） ----
    v = risk[valid]
    n = int(v.size)
    n_high = int((lvl == 3).sum())
    n_ext = int((lvl == 4).sum())

    masked = np.where(valid, risk, -np.inf)
    r, c = np.unravel_index(int(np.argmax(masked)), masked.shape)
    xs, ys = geom["transform"] * (c + 0.5, r + 0.5)      # 像元中心 -> 平面米
    from pyproj import Transformer
    lon, lat = Transformer.from_crs(4547, 4326, always_xy=True).transform(xs, ys)

    return {
        "date": day,
        "png": png_path.name,
        "bin": bin_path.name,
        "mean": round(float(v.mean()), 4),
        "median": round(float(np.median(v)), 4),
        "p90": round(float(np.percentile(v, 90)), 4),
        "max": round(float(v.max()), 4),
        "area_high_km2": round(n_high * CELL_KM2, 1),
        "area_extreme_km2": round(n_ext * CELL_KM2, 1),
        "cells": n,
        "no_data": int(risk.size - n),
        "top_cell": {"lon": round(float(lon), 5), "lat": round(float(lat), 5),
                     "value": round(float(v.max()), 4)},
    }


def show(path: Path) -> str:
    """打印用路径：项目内显示相对路径（短），项目外显示绝对路径（别抛异常）。"""
    try:
        return str(path.relative_to(PROJECT_DIR))
    except ValueError:
        return str(path)


def write_manifest(items: list[dict]) -> Path:
    """写 risk_timeline.json —— 前端的单一数据源。

    前端不许自己抄 bbox、不许自己写分级阈值、不许自己写颜色。
    一旦这里改了权重或分级，前端会自动跟着变；
    手抄的话一定会有一次忘了改，然后出现"图例说 0.79 是极高、
    地图上 0.72 就红了"这种谁都不信的图。
    """
    geom = geometry()
    ow, oh, _ = wgs84_geometry()
    from process.flood_index import CLICK_RES_DEG
    frames = sorted(items, key=lambda it: it["date"])
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layer": "dynamic_risk",
        "desc": "静态地形易涝指数 × 降雨强度放大系数（逐日）",
        "bbox_wgs84": geom["bbox_wgs84"],
        "bbox_order": "west, south, east, north",
        # 这两个数字描述的是**导出网格**（png / bin），别和下面的 stat_grid 混。
        "width": ow,
        "height": oh,
        "res_deg": round(float(CLICK_RES_DEG), 8),
        "crs": "EPSG:4326（等经纬度网格，与静态贴图、点击查询同源）",
        # 统计换了口径，必须明写，否则"cells 7,863,040"和 1656×1671 的图对不上，
        # 拿图去数格子的人会以为程序算错了。
        "stat_grid": {
            "width": geom["width"], "height": geom["height"], "cell_m": RES_M,
            "note": ("均值/分位/面积都在 30 m 原网格上统计（CELL_KM2 严格等于 900 m²）；"
                     "导出网格约 90 m，仅用于展示与点击。两套口径不可混用。"),
        },
        "level_file": "uint8, row-major, 无头, 0~4 等级, 255 无数据",
        "fixed_levels": "阈值取自静态指数，跨帧固定，便于比较不同日期的红区面积",
        "levels": [{"name": nm, "lo": round(float(lo), 4), "hi": round(float(hi), 4),
                    "color": cl} for lo, hi, nm, cl in LEVELS],
        "api_decay": API_DECAY,
        "api_dry_max": API_DRY_MAX,
        "api_wet_min": API_WET_MIN,
        "factor_max": FACTOR_MAX,
        "frames": frames,
    }
    path = OUT_DIR / "risk_timeline.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="动态风险栅格：静态地形 × 降雨强度")
    ap.add_argument("--start", default="2020-07-04", help="起始日期 YYYY-MM-DD（默认 2020-07-04）")
    ap.add_argument("--end", default="2020-07-10", help="结束日期 YYYY-MM-DD（默认 2020-07-10）")
    ap.add_argument("--only-test", action="store_true", help="只跑常量校验和自检")
    args = ap.parse_args()

    t_all = time.time()
    print("=" * 78)
    print("第 0 步：读配置 + 校验 TODO")
    print("=" * 78)
    load_env(ENV_FILE)                       # 复用 markov_rain 的 .env 读取
    check_todo()                             # 没填就 sys.exit，print 里已经说清该填哪个
    print("  ✓ 三个常量已填："
          f"API_DRY_MAX={API_DRY_MAX:,.1f} mm  API_WET_MIN={API_WET_MIN:,.1f} mm  "
          f"FACTOR_MAX={FACTOR_MAX:.2f}")

    print()
    print("=" * 78)
    print("第 1 步：自检（四项）")
    print("=" * 78)
    if not self_test():
        sys.exit("\n[中止] 自检没过，先看上面哪个 ✗。")
    if args.only_test:
        print(f"\n--only-test 结束，用时 {time.time() - t_all:.2f}s")
        return

    print()
    print("=" * 78)
    print("第 2 步：读雨量 + 算前期降雨指数")
    print("=" * 78)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    warm = start - timedelta(days=API_WARMUP_DAYS)        # 预热窗：别让第一天从 0 起算
    dates_all, codes, mm_all, lonlat = load_rain(warm.isoformat(), end.isoformat())
    api_all = api_series(mm_all)
    print(f"  站点 {len(codes)} 个：{' '.join(codes)}")
    print(f"  读取 {dates_all[0]} ~ {dates_all[-1]} 共 {len(dates_all):,} 天"
          f"（含 {API_WARMUP_DAYS} 天预热窗，用于把 API 递推热起来）")
    print(f"  5 站累计 {mm_all.sum():,.0f} mm（面雨量约 {mm_all.mean():,.0f} mm），"
          f"API 递推衰减 {API_DECAY}"
          f"（0.9^7 = {API_DECAY ** 7:.2f}，约一周衰减一半）")

    # 目标区间在整体序列里的下标
    keep = [i for i, d in enumerate(dates_all) if args.start <= d <= args.end]
    if not keep:
        sys.exit(f"[错误] 预热窗里没有 {args.start} ~ {args.end} 的日期，检查 --start/--end")

    print()
    print("=" * 78)
    print("第 3 步：静态指数 + 栅格几何")
    print("=" * 78)
    if not INDEX_NPY.exists():
        sys.exit(f"[错误] 找不到 {INDEX_NPY.name}，请先跑 process/flood_index.py")
    index_base = np.load(INDEX_NPY).astype(np.float32)
    geom = geometry()
    if index_base.shape != (geom["height"], geom["width"]):
        sys.exit(f"[错误] {INDEX_NPY.name} 形状 {index_base.shape} 与 tif "
                 f"{(geom['height'], geom['width'])} 不一致")
    # 市域裁剪 + 剔水体：复刻 M3 的"可评价陆地"，并把非陆地置成 NaN。
    # 不做这一步，贴图会铺满整个 1°×1° 分幅（含孝感、鄂州），
    # 湖心还会因为填洼因子最高而变成图上最红的地方。
    land, water = build_land_mask(index_base)
    index_base = np.where(land, index_base, np.nan).astype(np.float32)
    levels = ensure_levels(index_base, land)
    print(f"  栅格 {geom['width']} × {geom['height']} = {index_base.size:,} 格，"
          f"像元 {RES_M:.0f} m（{CELL_KM2:.4f} km²）")
    print(f"  可评价陆地 {int(land.sum()):,} 格 = {land.sum() * CELL_KM2:,.0f} km²"
          f"（另：水体 {int(water.sum()):,} 格 = {water.sum() * CELL_KM2:,.0f} km²，"
          f"其余为市域外/缺测 {int(index_base.size - land.sum() - water.sum()):,} 格）")
    print(f"  分级阈值按陆地静态指数取 p35/p65/p88/p97："
          f"{' / '.join(f'{lv[0]:.3f}' for lv in levels[1:])}")
    sxy = station_xy(lonlat, geom["transform"])
    print(f"  {'站号':<6}{'经度':>10}{'纬度':>10}{'列':>10}{'行':>10}")
    for code, (lon, lat), (col, row) in zip(codes, lonlat, sxy):
        print(f"  {code:<6}{lon:>10.4f}{lat:>10.4f}{col:>10.1f}{row:>10.1f}")

    print()
    print("=" * 78)
    print(f"第 4 步：逐日合成动态风险（{args.start} ~ {args.end}）")
    print("=" * 78)
    shape = (geom["height"], geom["width"])
    items: list[dict] = []
    for i in keep:
        day = dates_all[i]
        api_grid = idw_field(api_all[i], sxy, shape)         # 5 站 -> 全幅面
        factor = rain_factor(api_grid)                       # 面 -> 放大系数
        if not np.isfinite(factor).all():
            sys.exit(f"[错误] {day} 的 rain_factor 输出了 NaN，检查你的映射有没有除零")
        risk = combine(index_base, factor)                   # 静态 × 降雨
        risk = np.where(np.isfinite(index_base), risk, np.nan).astype(np.float32)

        item = export_snapshot(day, risk, index_base, levels)
        item["rain_mm"] = {c: round(float(mm_all[i, j]), 1) for j, c in enumerate(codes)}
        item["api_mean"] = round(float(api_all[i].mean()), 1)
        item["factor_min"] = round(float(np.nanmin(factor)), 3)
        item["factor_max"] = round(float(np.nanmax(factor)), 3)
        items.append(item)
        print(f"  {day}  面雨量 {mm_all[i].mean():6.1f} mm  "
              f"API均值 {api_all[i].mean():7.1f} mm  "
              f"系数 {item['factor_min']:.2f}~{item['factor_max']:.2f}  "
              f"风险均值 {item['mean']:.4f}  "
              f"高 {item['area_high_km2']:8,.1f} km²  "
              f"极高 {item['area_extreme_km2']:8,.1f} km²")

    print()
    print("=" * 78)
    print("第 5 步：汇总 manifest")
    print("=" * 78)
    path = write_manifest(items)
    print(f"  已写 {show(path)}（{len(items)} 帧，{path.stat().st_size / 1024:.1f} KB）")
    ow, oh, _ = wgs84_geometry()
    print(f"  导出网格 {ow} × {oh}（EPSG:4326，与静态贴图/点击查询同源）；"
          f"统计口径 30 m 原网格，见 manifest 的 stat_grid")
    print(f"  产物目录 {show(OUT_DIR)}：{len(list(OUT_DIR.glob('risk_*')))} 个文件")

    print()
    print("=" * 78)
    print(f"{'日期':<12}{'面雨量mm':>10}{'API均值':>10}{'系数范围':>14}"
          f"{'风险均值':>11}{'高风险km²':>12}{'极高km²':>11}")
    print("-" * 78)
    for it in sorted(items, key=lambda x: x["date"]):
        mm_day = sum(it["rain_mm"].values()) / len(it["rain_mm"])
        rng = f"{it['factor_min']:.2f}~{it['factor_max']:.2f}"
        print(f"{it['date']:<12}{mm_day:>10,.1f}{it['api_mean']:>10,.1f}"
              f"{rng:>14}{it['mean']:>11.4f}{it['area_high_km2']:>12,.1f}"
              f"{it['area_extreme_km2']:>11,.1f}")

    print()
    print("=" * 78)
    print(f"M5-3 完成，总用时 {time.time() - t_all:.1f}s")
    print("下一步：前端读 risk_timeline.json 播时间轴；risk_snapshot 表保持为空是设计。")
    print("=" * 78)


if __name__ == "__main__":
    main()
