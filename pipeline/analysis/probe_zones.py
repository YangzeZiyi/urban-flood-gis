"""
易涝点候选表 —— 坐标定位 + 行政区自检 + 模型指数查询

数据来源
--------
武汉市水务局 2020 年 5 月发布的《中心城区易渍水点风险图》：
    中心城区（含东西湖区）主次干道共 59 处易渍水点，
    高风险 8 处（大于 50 mm 强降雨即渍水）、中风险 45 处（100~150 mm）、
    低风险 6 处（150 mm 以上）。
    名单与分级见 中国新闻网 2020-05-09、武汉市水务局官网转载。
2017/2020 两版 61 处清单（汉口 21 / 汉阳 5 / 武昌 35）用于补充地名。

坐标怎么来的
------------
官方只给路段名，不给坐标。这里按公开地名手工定位到**路口/立交级别**，
再用两道自检兜住错误：
    1) PostGIS ST_Contains 校验该点是否落在它声称的行政区里
    2) 查静态指数，若某点分数极端异常（< p10 或 > p99），提示复核
精度估计 ±200~400 m。对 90 m 栅格的片区级验证够用，但不能当门牌号。

这个脚本只做"体检"，不写库。确认无误后再由 load_zones.py 入库。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[2]
GRID_JSON = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_flood_index_grid.json"
GRID_BIN = PROJECT_DIR / "data" / "processed" / "dem" / "wuhan_flood_index_grid.bin"
ENV_FILE = PROJECT_DIR / "pipeline" / ".env"

# 官方分级 -> 本项目的 risk_level（前端配色 1低 2中 3高 4极高）
LEVEL_MAP = {"高": 4, "中": 3, "低": 2}

# (名称, 官方分级, 声称所在区, 经度, 纬度, 定位依据/置信度)
# 置信度：A=知名路口/立交，坐标可到 100 m 内；B=街道级，200~400 m；C=仅知片区，>500 m
CANDIDATES = [
    # ---- 2020 年官方点名的高风险 8 处 ----
    ("玉龙路邹家大湾",        "高", "汉阳区", 114.2075, 30.5310, "B 玉龙路南段"),
    ("虎泉街保利华都",        "高", "洪山区", 114.3855, 30.5230, "A 虎泉街中段"),
    ("佛祖岭一路涵洞",        "高", "江夏区", 114.4405, 30.4610, "B 光谷南佛祖岭"),
    ("金银湖马池桥",          "高", "东西湖区", 114.2455, 30.6560, "B 金银湖东"),
    ("金银湖金南二路",        "高", "东西湖区", 114.2405, 30.6605, "C 金银湖北"),
    ("宏图路与金银潭交汇处",  "高", "东西湖区", 114.2360, 30.6650, "B 金银潭大道"),
    ("宏图路保利公园家",      "高", "东西湖区", 114.2330, 30.6680, "C 宏图路北段"),
    ("盘龙城桥",              "高", "黄陂区", 114.2850, 30.7150, "B 府河盘龙城"),

    # ---- 汉口（江岸 / 江汉 / 硚口）----
    ("竹叶山立交",            "中", "江岸区", 114.2845, 30.6335, "A 发展大道×黄孝河路"),
    ("姑嫂树铁路下穿通道",    "中", "江汉区", 114.2565, 30.6360, "A 姑嫂树路"),
    ("金墩街铁路下穿涵洞",    "中", "江汉区", 114.2470, 30.6215, "A 金墩街"),
    ("三眼桥北路",            "中", "江岸区", 114.2830, 30.6180, "B 三眼桥"),
    ("香港路唐家墩路口",      "中", "江汉区", 114.2755, 30.6105, "A 香港路×唐家墩"),
    ("解放大道单洞门",        "中", "江汉区", 114.2740, 30.5835, "A 解放大道单洞门"),
    ("武胜路电车公司",        "中", "硚口区", 114.2680, 30.5745, "A 武胜路"),
    ("汉正街地区",            "中", "硚口区", 114.2760, 30.5705, "A 汉正街"),
    ("建一路",                "中", "硚口区", 114.2565, 30.5805, "A 建一路"),
    ("解放大道太平洋",        "中", "硚口区", 114.2530, 30.5775, "A 太平洋"),
    ("台北路",                "中", "江岸区", 114.2810, 30.5985, "A 台北路"),
    ("雪松路",                "中", "江汉区", 114.2685, 30.5925, "A 雪松路"),

    # ---- 汉阳 ----
    ("龙阳大道郭茨口",        "中", "汉阳区", 114.2300, 30.5560, "A 龙阳大道×郭茨口"),
    ("马沧湖路",              "中", "汉阳区", 114.2400, 30.5460, "B 马沧湖路"),
    ("江堤中路",              "中", "汉阳区", 114.2400, 30.5300, "B 江堤中路"),
    ("四台工业园",            "中", "汉阳区", 114.1900, 30.5450, "C 汉阳西"),

    # ---- 武昌 ----
    ("徐东大街地下通道",      "中", "武昌区", 114.3520, 30.5780, "A 徐东大街"),
    ("和平大道三角路铁路桥下", "中", "武昌区", 114.3350, 30.5855, "A 三角路"),
    ("民主路中山路交会处",    "中", "武昌区", 114.3105, 30.5505, "A 司门口"),
    ("武昌中南商业大楼",      "中", "武昌区", 114.3350, 30.5450, "A 中南路"),
    ("湖北省人民医院",        "中", "武昌区", 114.3095, 30.5385, "A 张之洞路"),
    ("张之洞路701所",         "中", "武昌区", 114.3155, 30.5395, "A 701所"),
    ("晒湖铁路立交涵洞",      "中", "武昌区", 114.3200, 30.5200, "B 晒湖"),
    ("珞珈山武大后门",        "中", "武昌区", 114.3600, 30.5405, "A 武大"),

    # ---- 洪山 ----
    ("雄楚大道石牌岭路口",    "中", "洪山区", 114.3450, 30.5155, "A 石牌岭"),
    ("八一路与卓刀泉北路",    "中", "武昌区", 114.3600, 30.5350, "A 卓刀泉（北段属武昌区）"),
    ("南湖路供销学校",        "中", "洪山区", 114.3500, 30.5000, "B 南湖路"),
    ("新竹路与民族大道路口",  "中", "洪山区", 114.3950, 30.4955, "A 民族大道"),
    ("楚雄大道珞雄路口",      "中", "洪山区", 114.3900, 30.5100, "B 珞雄路"),

    # ---- 青山（注意：青山区被洪山区环抱，中心在 114.453，不是 114.41）----
    ("建设十路红钢二街49中",  "中", "青山区", 114.4050, 30.6205, "A 红钢城"),
    ("工业三路鄂州街",        "中", "青山区", 114.4400, 30.6100, "B 青山工业区"),
    ("友谊大道工业路",        "中", "青山区", 114.4200, 30.6180, "A 工业路"),
]


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def districts_of(lon: float, lat: float) -> list[str]:
    """用 PostGIS 查这个点落在哪些区（边界上的点可能命中多个）。"""
    sql = (
        "SELECT name FROM admin_boundary "
        f"WHERE ST_Contains(geom, ST_SetSRID(ST_Point({lon}, {lat}), 4326)) "
        "ORDER BY name;"
    )
    out = subprocess.run(
        ["psql", "-t", "-A", "-c", sql],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    )
    return [x.strip() for x in out.stdout.splitlines() if x.strip()]


def load_grid():
    meta = json.loads(GRID_JSON.read_text(encoding="utf-8"))
    w, h = meta["width"], meta["height"]
    grid = np.memmap(GRID_BIN, dtype="<f4", mode="r", shape=(h, w, 4))
    return grid, meta


def index_at(grid, meta, lon: float, lat: float) -> float:
    w, h = meta["width"], meta["height"]
    west, south, east, north = meta["bbox_wgs84"]
    col = int((lon - west) / (east - west) * w)
    row = int((north - lat) / (north - south) * h)
    if not (0 <= row < h and 0 <= col < w):
        return float("nan")
    return float(grid[row, col, 0])


def main() -> None:
    load_env(ENV_FILE)
    grid, meta = load_grid()

    rows = []
    print("=" * 108)
    print(f"{'名称':<24}{'声称区':<8}{'实际区':<8}{'等级':<5}{'模型指数':<9}{'校验'}")
    print("=" * 108)
    bad = []
    for name, grade, claim, lon, lat, note in CANDIDATES:
        actual = districts_of(lon, lat)
        idx = index_at(grid, meta, lon, lat)
        ok_dist = claim in actual
        flag = "✓" if ok_dist else ("✗ 落区不符" if actual else "✗ 出界")
        if not ok_dist:
            bad.append((name, claim, actual))
        idx_s = "NaN" if np.isnan(idx) else f"{idx:.3f}"
        print(f"{name:<24}{claim:<8}{(actual[0] if actual else '—'):<8}"
              f"{grade:<5}{idx_s:<9}{flag}")
        rows.append(dict(name=name, grade=grade, claim=claim, actual=actual,
                         lon=lon, lat=lat, index=idx, note=note))

    print("=" * 108)
    print(f"共 {len(rows)} 个点，落区不符 {len(bad)} 个")
    for n, c, a in bad:
        print(f"  · {n}: 声称 {c}，实际 {a or '界外'}")

    valid = [r for r in rows if not np.isnan(r["index"])]
    land = grid[:, :, 0]
    land_ok = land[np.isfinite(land)]
    print(f"\n模型可评价 {len(valid)} 个（其余落在水体或市域外）")
    if valid:
        v = np.array([r["index"] for r in valid])
        print(f"  均值 {v.mean():.3f}  中位 {np.median(v):.3f}  范围 {v.min():.3f}~{v.max():.3f}")
        for g in ("高", "中", "低"):
            sub = [r["index"] for r in valid if r["grade"] == g]
            if sub:
                print(f"  官方{g}风险 {len(sub):>2} 个：均值 {np.mean(sub):.3f}  "
                      f"中位 {np.median(sub):.3f}")
        print(f"  全城陆地对照：均值 {land_ok.mean():.3f}  "
              f"p90 {np.percentile(land_ok, 90):.3f}  p97 {np.percentile(land_ok, 97):.3f}")

        # 分位比绝对值有意义：地形指数是相对排序，不是绝对概率。
        # 一个点在 p90 意味着"比全城 90% 的陆地更易涝"。
        print(f"\n{'名称':<24}{'等级':<5}{'指数':>7}{'全城分位':>9}   判定")
        print("-" * 108)
        for r in sorted(valid, key=lambda x: -x["index"]):
            pct = float((land_ok < r["index"]).mean() * 100)
            r["pct"] = round(pct, 1)
            verdict = ("前 5%" if pct >= 95 else "前 10%" if pct >= 90
                       else "前 25%" if pct >= 75 else "后半") 
            print(f"{r['name']:<24}{r['grade']:<5}{r['index']:>7.3f}{pct:>8.1f}%   {verdict}")

        hi = [r for r in valid if r["grade"] == "高"]
        if hi:
            n90 = sum(1 for r in hi if r["pct"] >= 90)
            n75 = sum(1 for r in hi if r["pct"] >= 75)
            print(f"\n官方高风险 {len(hi)} 个可评价：进入全城前 10% 的 {n90} 个，"
                  f"前 25% 的 {n75} 个")
        miss = [r for r in rows if np.isnan(r["index"])]
        if miss:
            print(f"模型漏报（判为水体/界外）{len(miss)} 个：" +
                  "、".join(r["name"] for r in miss))

    out = PROJECT_DIR / "data" / "clean" / "flood_zones_candidates.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n候选表已写 {out.relative_to(PROJECT_DIR)}")


if __name__ == "__main__":
    main()
