# -*- coding: utf-8 -*-
"""
markov_rain.py —— 日降雨的五态马尔科夫链
===============================================================================
问题：明天会不会下暴雨？

最笨的答法是"看历史频率"——武汉 8 年里 22 天暴雨，占 0.75%，所以天天答 0.75%。
但下雨有记忆：今天在下大雨，明天大概率还在下；今天晴，明天大概率还晴。
马尔科夫链就是用"当前状态"修正概率的那套工具：

        明天状态的概率 = 只取决于今天状态（一阶马尔科夫假设）

        状态空间 S = {无雨, 小雨, 中雨, 大雨, 暴雨}
        转移矩阵 P[i][j] = 今天 i 态、明天 j 态的频率

有了 P，从今天的状态出发乘 n 次，就得到未来第 n 天的状态分布；
把 7 天的分布叠起来，就是"未来一周暴雨概率"——这是风险模型的时间输入。

数据：rain_record 里 2017–2024 的日雨量（fetch/fetch_rain.py 入库），默认汉口站。

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python analysis/markov_rain.py
    python analysis/markov_rain.py --date 2020-07-04      # 回放那场雨
    python analysis/markov_rain.py --station WH04         # 江夏站

依赖：psycopg2（已在 requirements.txt）
===============================================================================
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import psycopg2

PIPELINE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PIPELINE_DIR / ".env"

# 五态划分：按国家气象标准（日雨量，毫米）
#   小雨 <10、中雨 10~25、大雨 25~50、暴雨 ≥50；<0.1 记作无雨
# 为什么不用分位数切？因为"暴雨"是有法规含义的词（50 mm），
# 用分位数切出来的"暴雨"没法跟气象台的预警对话。
STATES = [
    ("无雨", 0.0, 0.1),
    ("小雨", 0.1, 10.0),
    ("中雨", 10.0, 25.0),
    ("大雨", 25.0, 50.0),
    ("暴雨", 50.0, float("inf")),
]
N = len(STATES)


# ---------------------------------------------------------------------------
def load_env(path: Path) -> None:
    if not path.exists():
        sys.exit(f"[错误] 找不到配置文件：{path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


def load_series(station: str) -> tuple[list[str], np.ndarray]:
    """按时间顺序读一个站的日雨量序列。

    必须 ORDER BY obs_time：马尔科夫链算的是"相邻两天"的转移，
    顺序错了整张矩阵就变成了随机数。
    """
    with psycopg2.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.obs_time::date, r.rainfall_mm
                  FROM rain_record r
                  JOIN rain_station s ON s.id = r.station_id
                 WHERE s.code = %s
                 ORDER BY r.obs_time
                """,
                (station,),
            )
            rows = cur.fetchall()
    if not rows:
        sys.exit(f"[错误] 没有 {station} 的数据，先跑 fetch/fetch_rain.py")

    dates = [str(r[0]) for r in rows]
    mm = np.array([float(r[1] or 0.0) for r in rows], dtype=np.float64)
    return dates, mm


def classify(mm: np.ndarray) -> np.ndarray:
    """雨量 -> 状态编号 0..4。

    用 np.digitize 而不是循环：边界数组 [0.1, 10, 25, 50] 走一遍就分好类，
    277 万像元那套东西学过一次，这里顺手就能用上。
    """
    edges = [lo for _name, lo, _hi in STATES[1:]]     # [0.1, 10, 25, 50]
    return np.digitize(mm, edges, right=False)


# ---------------------------------------------------------------------------
# ★ TODO 1：统计转移频次矩阵 ★
# ---------------------------------------------------------------------------
def count_transitions(states: np.ndarray) -> np.ndarray:
    """返回 5×5 整数矩阵 counts，counts[i][j] = "昨天 i 态、今天 j 态"出现过的次数。

    要填的只有循环体里两件事：
      (1) 取出相邻两天：for t in range(len(states) - 1):
      (2) 在矩阵对应位置 +1：counts[ 昨天的状态 ][ 今天的状态 ] += 1
    最后一天没有"明天"，循环到 len-1 就停，别越界。

    提示：本文件顶部已经 import numpy as np，zeros 的写法是
          counts = np.zeros((N, N), dtype=np.int64)
    """
    counts = np.zeros((N, N), dtype=np.int64)

    # ---- 在这里写你的两行 ----
    # TODO ★★★

    return counts


# ---------------------------------------------------------------------------
# ★ TODO 2：把频次矩阵按行归一化成概率矩阵 ★
# ---------------------------------------------------------------------------
def normalize_rows(counts: np.ndarray) -> np.ndarray:
    """counts[i][j] / counts[i].sum() -> P[i][j]，每一行加起来必须等于 1。

    两个坑：
      (1) 某个状态一次都没出现过时，这一行全是 0，除下去会得到 NaN。
          本数据集五态都有，但你得想清楚"万一没有"该怎么处理，写一行注释说明。
      (2) 要用浮点除。Python 里 3/2 = 1.5，但 numpy 的整数数组相除要注意
          先 astype(float)，否则在某些写法下会整除。
    """
    P = np.zeros((N, N), dtype=np.float64)

    # ---- 在这里写你的归一化 ----
    # TODO ★★★

    return P


# ---------------------------------------------------------------------------
# 以下是跑通的工具函数，不需要你改
# ---------------------------------------------------------------------------
def matrix_power(P: np.ndarray, n: int) -> np.ndarray:
    """P 的 n 次幂：n 步之后各状态的概率分布。"""
    R = np.eye(N)
    for _ in range(n):
        R = R @ P
    return R


def stationary(P: np.ndarray, tol: float = 1e-12, max_iter: int = 5000) -> np.ndarray:
    """平稳分布 π：满足 π = πP 的概率向量。

    做法是最朴素的幂迭代：从均匀分布出发，一直乘 P，直到不再变化。
    数学上叫"马尔科夫链的极限分布"，物理意义是"不管今天什么天气，
    很多天以后各状态出现的比例"。
    """
    pi = np.full(N, 1.0 / N)
    for _ in range(max_iter):
        nxt = pi @ P
        if np.abs(nxt - pi).max() < tol:
            return nxt
        pi = nxt
    return pi


def self_test() -> bool:
    """拿一个能手算的玩具序列验证矩阵：0 0 1 0 0 1（只有 0/1 两态）。

    相邻对：(0,0) (0,1) (1,0) (0,0) (0,1)
    期望：counts[0] = [2, 2]，counts[1] = [1, 0]；P[0] = [0.5, 0.5]，P[1] = [1, 0]
    """
    toy = np.array([0, 0, 1, 0, 0, 1])
    c = count_transitions(toy)
    P = normalize_rows(c)

    ok = (c[0, 0] == 2 and c[0, 1] == 2 and c[1, 0] == 1 and c[1, 1] == 0
          and abs(P[0, 0] - 0.5) < 1e-9 and abs(P[1, 0] - 1.0) < 1e-9)
    print("=" * 70)
    print("自检：玩具序列 0 0 1 0 0 1")
    print("=" * 70)
    print(f"  counts[0] = {c[0]}  （期望 [2 2 0 0 0]）")
    print(f"  counts[1] = {c[1]}  （期望 [1 0 0 0 0]）")
    print(f"  P[0]      = {np.round(P[0], 3)}  （期望 [0.5 0.5 0 0 0]）")
    print(f"  {'✓ 通过' if ok else '✗ 不通过 —— 先修 TODO 1 / 2 再看后面的结果'}")
    return ok


def report(dates: list[str], mm: np.ndarray, states: np.ndarray,
           counts: np.ndarray, P: np.ndarray, focus: str | None) -> None:
    """把矩阵、持续性、平稳分布、7 天展望一次打出来。"""
    names = [s[0] for s in STATES]

    print("\n" + "=" * 70)
    print(f"转移矩阵 P（{dates[0]} ~ {dates[-1]}，共 {len(states):,} 天）")
    print("=" * 70)
    print("  行 = 今天，列 = 明天；每行加起来 = 1")
    print("        " + "".join(f"{n:>7}" for n in names) + f"{'行和':>8}")
    for i in range(N):
        cells = "".join(f"{P[i, j]:7.3f}" for j in range(N))
        print(f"  {names[i]:<4}{cells}{P[i].sum():8.2f}")

    # 次数很少的状态行不可信，得说清楚
    print("\n  样本量（每种状态出现过的天数）：")
    print("    " + "  ".join(f"{names[i]} {int((states == i).sum()):>4}"
                             for i in range(N)))

    print("\n" + "=" * 70)
    print("这场雨会下几天？—— 平均持续时间 1/(1-P[i][i])")
    print("=" * 70)
    for i in range(N):
        p_stay = P[i, i]
        if p_stay >= 1:
            print(f"  {names[i]:<4} 永远不转出（样本只有这一段，别当真）")
        else:
            print(f"  {names[i]:<4} 平均持续 {1 / (1 - p_stay):5.2f} 天"
                  f"（停留概率 {p_stay:.3f}）")

    print("\n" + "=" * 70)
    print("检验：平稳分布 vs 实际频率（差太多说明链不平稳）")
    print("=" * 70)
    pi = stationary(P)
    freq = np.array([(states == i).mean() for i in range(N)])
    print(f"  {'状态':<6}{'平稳分布':>10}{'实际频率':>10}")
    for i in range(N):
        print(f"  {names[i]:<6}{pi[i] * 100:9.2f}%{freq[i] * 100:9.2f}%")

    # ---- 7 天展望 ----
    if focus is None:
        focus = dates[-1]
    if focus in dates:
        start = dates.index(focus)
    else:
        # 允许给未来日期：从最后一天的状态出发
        start = len(dates) - 1

    cur = states[start]
    print("\n" + "=" * 70)
    print(f"7 天展望：从 {dates[start]}（{names[cur]}，{mm[start]:.1f} mm）出发")
    print("=" * 70)
    print(f"  {'天数':<6}{'无雨':>7}{'小雨':>7}{'中雨':>7}{'大雨':>7}{'暴雨':>7}"
          f"{'实际':>9}")
    for n in range(1, 8):
        dist = matrix_power(P, n)[cur]
        if start + n < len(dates):
            actual = f"{mm[start + n]:6.1f}"
        else:
            actual = "   —"
        print(f"  {n:>2} 天" + "".join(f"{dist[j] * 100:7.1f}" for j in range(N))
              + f"{actual:>9}")

    # "7 天内至少出现一次暴雨"要在链上精确算，不能把每天当独立事件乘起来。
    # 做法：把暴雨那一行/列切掉，得到"没有暴雨"的子矩阵 Q，
    # 从当前状态出发走 7 步还留在子空间里的概率，就是"7 天都不暴雨"。
    keep = [i for i in range(N) if i != N - 1]
    Q = P[np.ix_(keep, keep)]
    if cur == N - 1:
        p_avoid = 0.0
    else:
        v0 = np.zeros(N - 1)
        v0[keep.index(cur)] = 1.0
        p_avoid = float((v0 @ np.linalg.matrix_power(Q, 7)).sum())
    print(f"\n  未来 7 天至少有一天下暴雨的概率：{(1 - p_avoid) * 100:.1f}%")
    print("  算法：在转移矩阵上精确求「7 天都不进暴雨态」的补概率，")
    print("        不是把每天当独立事件——连雨天不独立，乘起来会算错。")

    if start + 7 < len(dates):
        real = mm[start + 1:start + 8]
        print(f"  实际后 7 天雨量：{' '.join(f'{v:.0f}' for v in real)} mm"
              f"　暴雨 {int((real >= 50).sum())} 天")
        print("  ▲ 最后一列是给眼睛的对照，不是模型输入。气候态频率只能回答")
        print("    「历史上这种天气之后通常怎样」，答不了「这次低涡往哪走」。")


def main() -> None:
    ap = argparse.ArgumentParser(description="日降雨五态马尔科夫链")
    ap.add_argument("--station", default="WH01", help="站点编号，默认 WH01 汉口")
    ap.add_argument("--date", default=None, help="从哪天开始展望，默认最后一天")
    args = ap.parse_args()

    load_env(ENV_FILE)

    if not self_test():
        sys.exit(1)

    dates, mm = load_series(args.station)
    states = classify(mm)
    print(f"\n已读 {args.station}：{len(dates):,} 天，"
          f"{dates[0]} ~ {dates[-1]}，累计 {mm.sum():,.0f} mm")

    counts = count_transitions(states)
    P = normalize_rows(counts)
    report(dates, mm, states, counts, P, args.date)


if __name__ == "__main__":
    main()
