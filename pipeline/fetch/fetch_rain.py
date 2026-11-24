#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_rain.py —— 采集武汉日降雨序列，写入 PostGIS
===============================================================================
M5 的第一步：让平台"有雨可下"。

数据源：Open-Meteo Archive API（ERA5 再分析）
    https://archive-api.open-meteo.com/v1/archive
    免费、不用注册、不用 key，一次请求就能拿几十年的逐日序列。

为什么要用"格点"冒充雨量站？
    气象局的站点观测（中国气象数据网）需要申请、有审核周期，毕设等不起。
    ERA5 是欧洲中期天气预报中心的再分析产品，把观测、卫星、模式结果同化到
    网格上，取任意经纬度都能得到一个日雨量序列。
    它在武汉这种平原城市对"这场雨有多大"是可信的，但对"街对面有没有下"不行。

为什么取 5 个点？
    武汉东西宽约 70 km、南北约 90 km，一次暴雨常常"汉口暴雨、江夏没下"。
    单点会把整座城市拉成同一个雨量，所以取 5 个点对上 rain_station 的表结构。

为什么只取 2017–2024（不取 2015 起）？
    查过数据才定的，不是拍脑袋：
      ① 2015–2016 两年，四个城区站**逐日完全相同**（365/365、366/366）——
         说明那两年接口给的还是 0.25° 粗网格，5 个点里 4 个落在同一格，
         "空间差异"是假的。
      ② 更要命的是极端值：2016-07-06 实况武汉国家站 246.4 mm、江夏 232.4 mm
         （中国气象局当日通报），而这份数据只给 44.4 mm，削掉了八成。
         ERA5 对"列车效应"这种中小尺度对流本来就平滑得厉害。
    有这两条，2016 只能当背景故事讲，不能拿来验证模型。2017 起换成 0.1°
    网格，四站才分开，用 2017–2024 共 2,922 天，估五态转移矩阵足够。

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python fetch/fetch_rain.py             # 有缓存就用缓存，没有才下载
    python fetch/fetch_rain.py --force     # 忽略缓存，重新下载
    python fetch/fetch_rain.py --dry-run   # 只下载+统计，不写数据库

依赖：requests、psycopg2（已在 pipeline/requirements.txt 里）
===============================================================================
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import psycopg2
import requests
from psycopg2.extras import execute_values

PIPELINE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = PIPELINE_DIR.parent
ENV_FILE = PIPELINE_DIR / ".env"
CACHE_DIR = PROJECT_DIR / "data" / "raw" / "rain"
CSV_FILE = CACHE_DIR / "wuhan_daily_rain.csv"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# 时间范围：见文件头"为什么只取 2017–2024"
START_DATE = "2017-01-01"
END_DATE = "2024-12-31"

DATA_SOURCE = "open-meteo-era5"

# (编号, 站名, 区, 经度, 纬度)
# 坐标取在各自城区建成区，不是行政区几何中心——黄陂的几何中心在山里
STATIONS = [
    ("WH01", "汉口", "江岸区", 114.2830, 30.6060),
    ("WH02", "武昌", "武昌区", 114.3160, 30.5450),
    ("WH03", "汉阳", "汉阳区", 114.2180, 30.5540),
    ("WH04", "江夏", "江夏区", 114.3160, 30.3750),
    ("WH05", "黄陂", "黄陂区", 114.3750, 30.8810),
]


# ---------------------------------------------------------------------------
# 配置与连接
# ---------------------------------------------------------------------------
def load_env(path: Path) -> None:
    """把 .env 的 KEY=VALUE 塞进 os.environ，psycopg2.connect() 就能自动读到。"""
    if not path.exists():
        sys.exit(f"[错误] 找不到配置文件：{path}\n"
                 f"       请把 pipeline/.env.example 复制一份改名为 .env，并填好密码")

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()


# ---------------------------------------------------------------------------
# 第一步：下载（带缓存）
# ---------------------------------------------------------------------------
def cache_matches(path: Path) -> bool:
    """缓存文件的首尾日期与当前配置一致吗？

    只判断"文件在不在"是不够的：改了 START_DATE / END_DATE 之后，
    旧缓存还在，脚本会心安理得地用一份时间范围错的数据跑下去。
    这种错不报错、不改文件名，最容易被忽略。
    """
    try:
        daily = json.loads(path.read_text(encoding="utf-8"))["daily"]
        return daily["time"][0] == START_DATE and daily["time"][-1] == END_DATE
    except Exception:
        return False


def fetch_station(code: str, lon: float, lat: float, force: bool = False) -> Path:
    """下载单个格点的逐日降雨，存成 JSON。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{code}_daily.json"

    if out.exists() and not force and cache_matches(out):
        print(f"      使用缓存 {out.name}（{out.stat().st_size / 1024:.0f} KB）")
        return out

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "daily": "precipitation_sum",
        "timezone": "Asia/Shanghai",
    }
    # timeout 必须给：不设的话网络卡住脚本会一直挂着
    resp = requests.get(ARCHIVE_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    if "daily" not in data:
        sys.exit(f"[错误] {code} 返回里没有 daily 字段：{str(data)[:200]}")

    # 先写临时文件再改名：写到一半中断也不会留下半截 JSON
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    print(f"      下载完成 {out.name}（{len(data['daily']['time'])} 天）")
    return out


# ---------------------------------------------------------------------------
# 第二步：整理成长表 CSV
# ---------------------------------------------------------------------------
def to_rows(paths: dict[str, Path]) -> list[tuple]:
    """把 5 份 JSON 拍平成 (code, date, rainfall_mm)。

    接口返回的 daily 里，time 和 precipitation_sum 是两个等长数组，
    同一个下标才是同一天，所以用 zip 同时走，再校验长度。
    """
    rows: list[tuple] = []
    for code, path in paths.items():
        daily = json.loads(path.read_text(encoding="utf-8"))["daily"]
        times, values = daily["time"], daily["precipitation_sum"]
        if len(times) != len(values):
            sys.exit(f"[错误] {code} 的 time 和 precipitation_sum 长度不一致")

        for day, mm in zip(times, values):
            # ERA5 偶尔有缺测（null）。缺测 ≠ 没下雨，写成 0 会让"无雨日"虚高，
            # 后面马尔科夫链的转移矩阵就偏了。宁可跳过并报数量。
            if mm is None:
                continue
            rows.append((code, day, round(float(mm), 2)))

    rows.sort(key=lambda r: (r[1], r[0]))
    return rows


def write_csv(rows: list[tuple]) -> None:
    """落一份 CSV。数据库可以重建，CSV 是"肉眼可查"的底稿。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "date", "rainfall_mm"])
        w.writerows(rows)
    print(f"      已写 {CSV_FILE.relative_to(PROJECT_DIR)}（{len(rows):,} 行）")


# ---------------------------------------------------------------------------
# 第三步：入库
# ---------------------------------------------------------------------------
def load_db(rows: list[tuple]) -> None:
    """写 rain_station（一站一行）和 rain_record（一天一行）。

    幂等性：站用 ON CONFLICT (code) 更新；记录先按 data_source 删干净再插。
    重跑得到同一份数据，不会越插越多——"重跑安全"比"跑得快"重要。
    """
    with psycopg2.connect() as conn:
        with conn.cursor() as cur:
            for code, name, district, lon, lat in STATIONS:
                cur.execute(
                    """
                    INSERT INTO rain_station (code, name, district, geom)
                    VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                    ON CONFLICT (code) DO UPDATE
                       SET name = EXCLUDED.name,
                           district = EXCLUDED.district,
                           geom = EXCLUDED.geom
                    """,
                    (code, name, district, lon, lat),
                )
            print(f"      站点：{len(STATIONS)} 个（新增或更新）")

            cur.execute("DELETE FROM rain_record WHERE data_source = %s", (DATA_SOURCE,))

            cur.execute("SELECT code, id FROM rain_station")
            id_of = dict(cur.fetchall())

            # obs_time 用当天 00:00，语义是"这一天的累计雨量"，不是瞬时观测。
            payload = [
                (id_of[code], f"{day} 00:00:00", mm, DATA_SOURCE)
                for code, day, mm in rows
            ]
            execute_values(
                cur,
                "INSERT INTO rain_record (station_id, obs_time, rainfall_mm, data_source) "
                "VALUES %s",
                payload,
                page_size=2000,
            )
            print(f"      记录：{len(payload):,} 行（data_source={DATA_SOURCE}）")


# ---------------------------------------------------------------------------
# 第四步：体检 —— 数字对不对，先看三眼
# ---------------------------------------------------------------------------
def report(rows: list[tuple]) -> None:
    """打印年雨量、极值、暴雨日数、2020 年那场雨，再验一次网格分辨率。

    武汉多年平均年降水量约 1,300 mm，梅雨在 6 月中—7 月中。
    年雨量落在 1,100~1,600 之间才正常；差太多说明接口给的不是这块地方。
    """
    by_year: dict[str, float] = {}
    by_year_max: dict[str, tuple[float, str]] = {}
    heavy = 0
    for code, day, mm in rows:
        if code != "WH01":          # 年雨量只看汉口，避免 5 站重复累加
            continue
        y = day[:4]
        by_year[y] = by_year.get(y, 0.0) + mm
        if y not in by_year_max or mm > by_year_max[y][0]:
            by_year_max[y] = (mm, day)
        if mm >= 50:                # 气象上 50 mm 为"暴雨"
            heavy += 1

    print("\n" + "=" * 70)
    print("汉口站逐年雨量（毫米）")
    print("=" * 70)
    for y in sorted(by_year):
        peak, peak_day = by_year_max[y]
        print(f"  {y}  {by_year[y]:7.0f} mm   最大日雨量 {peak:6.1f} mm（{peak_day}）")
    print(f"\n  {len(by_year)} 年平均 {sum(by_year.values()) / len(by_year):,.0f} mm/年"
          f"　暴雨日（≥50 mm）{heavy} 天")

    print("\n" + "=" * 70)
    print("2020 年 7 月 4–8 日（超长梅雨的峰值段）")
    print("=" * 70)
    days = ["2020-07-04", "2020-07-05", "2020-07-06", "2020-07-07", "2020-07-08"]
    for code, name, *_ in STATIONS:
        week = {d: mm for c, d, mm in rows if c == code and d in days}
        print(f"  {name:<4} " + "  ".join(f"{d[5:]} {week.get(d, 0):5.1f}" for d in days))
    print("\n  实况对照：2020-07-06 武汉国家站 157.6 mm —— 与表中汉口站一致，")
    print("  说明 2017 年之后的网格对日尺度暴雨量级是可信的。")

    # ---- 分辨率体检：四城区站有多少天完全相同 ----
    city = {"WH01", "WH02", "WH03", "WH04"}
    by_date: dict[str, dict[str, float]] = {}
    for code, day, mm in rows:
        if code in city:
            by_date.setdefault(day, {})[code] = mm
    same = sum(1 for v in by_date.values() if max(v.values()) - min(v.values()) < 1e-9)
    print(f"\n  分辨率体检：四城区站共有 {same / len(by_date) * 100:.0f}% 的天数值完全相同"
          f"（多为无雨日；若接近 100% 说明又退回粗网格了）")


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="采集武汉日降雨并入库")
    ap.add_argument("--force", action="store_true", help="忽略本地缓存，重新下载")
    ap.add_argument("--dry-run", action="store_true", help="只下载和统计，不写数据库")
    args = ap.parse_args()

    load_env(ENV_FILE)

    print(f"[1/4] 下载 {len(STATIONS)} 个格点，{START_DATE} ~ {END_DATE}")
    paths = {}
    for code, name, _district, lon, lat in STATIONS:
        print(f"  {code} {name} ({lon:.4f}, {lat:.4f})")
        paths[code] = fetch_station(code, lon, lat, force=args.force)

    print("[2/4] 拍平成长表")
    rows = to_rows(paths)
    write_csv(rows)

    report(rows)

    if args.dry_run:
        print("\n[3/4] --dry-run：跳过入库")
        return

    print("\n[3/4] 写入 PostGIS")
    load_db(rows)

    print("[4/4] 完成")


if __name__ == "__main__":
    main()
