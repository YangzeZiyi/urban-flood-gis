"""
把官方易涝点写进 flood_zone 表

数据来源
--------
武汉市水务局 2020 年 5 月《中心城区易渍水点风险图》：中心城区（含东西湖区）
主次干道 59 处，高风险 8 处（>50 mm 降雨即渍水）、中风险 45 处（100~150 mm）、
低风险 6 处（>150 mm）。名单见 中国新闻网 2020-05-09 与水务局官网转载。

这里入库 40 处，是其中地名可定位的部分（高风险 8 处全收）。
坐标由公开地名手工定位到路口/立交级别，精度约 ±300 m，
并用 PostGIS ST_Contains 逐个校验过行政区（见 probe_zones.py）。

为什么要替换掉第一周那条测试数据
--------------------------------
id=1「武昌火车站下穿通道」是验证 PG→Spring→Cesium 链路时手插的：
几何是 2 km × 2.2 km 的方块，area_m2 却写 12000，与真实下穿通道尺度差 300 倍。
演示时被问「这个范围怎么来的」会很难答。现在用真数据替掉它。

几何为什么用 100 m 缓冲
-----------------------
flood_zone.geom 是 Polygon（区域），不是 Point。一个渍水点实际影响的是
一段路口/一个涵洞片区，用半径 100 m 的圆近似（约 3.1 公顷），
与官方描述的"路段"尺度相当。缓冲区用 geography 计算，
这样半径真的是 100 米，不会被纬度拉伸。

幂等
----
重复跑不会重复插入：先按 description 前缀删掉上次导入的，再插。
第一周的测试数据(id=1)单独按名字删一次。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = Path(__file__).resolve().parents[2]
CAND_JSON = PROJECT_DIR / "data" / "clean" / "flood_zones_candidates.json"
ENV_FILE = PROJECT_DIR / "pipeline" / ".env"

SRC_TAG = "来源：武汉市水务局 2020 年中心城区渍水风险图"
BUFFER_M = 100.0

# 官方分级 -> (本项目 risk_level, 触发雨量 mm)
# 前端配色：1 低(蓝) 2 中(黄) 3 高(橙) 4 极高(红)
GRADE_CONF = {
    "高": (4, 50.0),     # >50 mm 即渍水
    "中": (3, 100.0),    # 100~150 mm
    "低": (2, 150.0),    # >150 mm
}


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def model_note(row: dict) -> str:
    """把模型评分写进 description —— 它将来就是前端卡片上那段说明。"""
    idx = row.get("index")
    if idx is None or idx != idx:                       # NaN 的写法：x != x
        return ("模型判为水体或市域外，未给出评分"
                "（坡度 0.0°、填深 3 m 以上，地形上与湖面无法区分）——"
                "属已知漏报，见 pipeline/analysis/probe_zones.py")
    pct = row.get("pct")
    tail = f"，全城 {pct:.0f}% 分位" if isinstance(pct, (int, float)) else ""
    return f"模型评分 {idx:.3f}{tail}"


def main() -> None:
    load_env(ENV_FILE)
    rows = json.loads(CAND_JSON.read_text(encoding="utf-8"))
    if not rows:
        sys.exit("[错误] 候选表是空的，先跑 probe_zones.py")

    records = []
    for r in rows:
        grade = r["grade"]
        if grade not in GRADE_CONF:
            sys.exit(f"[错误] 未知分级 {grade}（{r['name']}）")
        level, rain = GRADE_CONF[grade]
        desc = (f"{SRC_TAG}（{grade}风险，>{rain:.0f} mm 降雨易渍水）。"
                f"坐标手工定位，精度约 ±300 m。{model_note(r)}。")
        records.append((r["name"], level, rain, desc, r["lon"], r["lat"]))

    sql_delete_test = (
        "DELETE FROM flood_zone "
        "WHERE id = 1 AND name = '武昌火车站下穿通道'"
    )
    sql_delete_prev = "DELETE FROM flood_zone WHERE description LIKE %s"

    sql_insert = """
        INSERT INTO flood_zone (name, risk_level, max_depth, area_m2, rain_mm, description, geom)
        VALUES %s
    """
    # ST_Buffer 走 geography：半径 100 米就是 100 米，不会被纬度拉长。
    # 不写 max_depth：官方只给风险等级，不给水深，编一个数字反而经不起追问。
    template = (
        "(%s, %s, NULL,"
        " ST_Area(ST_Buffer(ST_SetSRID(ST_Point(%s, %s), 4326)::geography, "
        f"{BUFFER_M})::geometry::geography), "
        "%s, %s,"
        " ST_Buffer(ST_SetSRID(ST_Point(%s, %s), 4326)::geography, "
        f"{BUFFER_M})::geometry)"
    )

    with psycopg2.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_delete_test)
            n_test = cur.rowcount
            cur.execute(sql_delete_prev, (SRC_TAG + "%",))
            n_prev = cur.rowcount
            print(f"清理：第一周测试数据 {n_test} 条，上次导入 {n_prev} 条")

            # 逐条插，因为 geom 和 area 都要用经纬度各算一次，用 values 模板更直白
            for name, level, rain, desc, lon, lat in records:
                cur.execute(
                    """
                    INSERT INTO flood_zone
                        (name, risk_level, max_depth, area_m2, rain_mm, description, geom)
                    VALUES
                        (%s, %s, NULL,
                         ST_Area(ST_Buffer(ST_SetSRID(ST_Point(%s,%s),4326)::geography,
                                           %s)::geometry::geography),
                         %s, %s,
                         ST_Buffer(ST_SetSRID(ST_Point(%s,%s),4326)::geography,
                                   %s)::geometry)
                    """,
                    (name, level, lon, lat, BUFFER_M, rain, desc, lon, lat, BUFFER_M),
                )
            conn.commit()

            cur.execute("""
                SELECT risk_level, count(*),
                       round(sum(area_m2)::numeric / 1e6, 2) AS km2,
                       round(avg(ST_Area(geom::geography))::numeric, 0) AS avg_m2
                  FROM flood_zone GROUP BY risk_level ORDER BY risk_level DESC
            """)
            print("\n入库统计（risk_level 4=极高 3=高 2=中）：")
            for lv, cnt, km2, avg in cur.fetchall():
                print(f"  等级 {lv}：{cnt:>2} 个，合计 {km2} km²，平均 {avg} m²/个")

            cur.execute("SELECT count(*), round(sum(area_m2)::numeric/1e6,3) FROM flood_zone")
            print(f"  总计 {cur.fetchone()[0]} 个")

            cur.execute("""
                SELECT name, risk_level, round(ST_X(ST_Centroid(geom))::numeric,4),
                       round(ST_Y(ST_Centroid(geom))::numeric,4)
                  FROM flood_zone ORDER BY risk_level DESC, name LIMIT 5
            """)
            print("\n抽样核对（等级 / 经度 / 纬度）：")
            for n, lv, lon, lat in cur.fetchall():
                print(f"  {lv}  {lon}, {lat}  {n}")


if __name__ == "__main__":
    main()
