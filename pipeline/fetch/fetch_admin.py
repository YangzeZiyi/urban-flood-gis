#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_admin.py —— 采集武汉市及各区县行政边界，写入 PostGIS
===============================================================================
这是整个数据管线的第一步：把"真实地理边界"装进数据库。
之后所有分析（哪个区积水多、易涝区落在哪个街道）都靠它做空间关联。

数据源：阿里云 DataV.GeoAtlas
    https://geo.datav.aliyun.com/areas_v3/bound/420100_full.json
    免费、无需注册、无需 key，返回标准 GeoJSON。
    420100 是武汉市的行政区划代码（adcode），规律是：
        42 = 湖北省，00 = 市本级，所以 420100 = 武汉市
    末尾加 _full 表示"连同下级区县一起返回"，不加则只返回市级轮廓。

用法：
    cd D:\\wb\\urban-flood-gis\\pipeline
    python fetch/fetch_admin.py            # 下载并入库
    python fetch/fetch_admin.py --force    # 忽略本地缓存，重新下载

依赖：requests、psycopg2（已在 pipeline/requirements.txt 里）
===============================================================================
"""

import json
import os
import sys
from pathlib import Path

import psycopg2
import requests

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
# __file__ 是当前脚本的路径。连续两次 .parent 就回到了 pipeline/ 目录。
# 用"相对脚本位置"而不是写死 D:\... ，是为了换台电脑也能跑。
PIPELINE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = PIPELINE_DIR.parent
ENV_FILE = PIPELINE_DIR / ".env"
CACHE_DIR = PROJECT_DIR / "data" / "raw" / "admin"
CACHE_FILE = CACHE_DIR / "wuhan_420100_full.json"

# 武汉市的行政区划代码，以及它的下载地址
WUHAN_ADCODE = "420100"
DATAV_URL = f"https://geo.datav.aliyun.com/areas_v3/bound/{WUHAN_ADCODE}_full.json"


# ---------------------------------------------------------------------------
# 第一步：读取 .env 里的数据库配置
# ---------------------------------------------------------------------------
def load_env(path: Path) -> None:
    """
    把 .env 里的 KEY=VALUE 逐行塞进 os.environ。

    为什么不用 python-dotenv 这个现成库？
        因为它需要额外装一个依赖。这个解析逻辑只有 6 行，
        自己写反而更容易看懂，也少一个要维护的包。

    注意 os.environ 是"进程级"的：一旦设置，本进程内的 psycopg2 就能读到。
    """
    if not path.exists():
        sys.exit(
            f"[错误] 找不到配置文件：{path}\n"
            f"       请把 pipeline/.env.example 复制一份改名为 .env，并填好密码"
        )

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 跳过空行和注释行
        if not line or line.startswith("#"):
            continue
        # 只处理 key=value 形式的行；split('=', 1) 表示只切一刀，
        # 这样密码里即使含 '=' 也不会被切坏
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip()

    print(f"[1/5] 已加载数据库配置：{path.name}")


# ---------------------------------------------------------------------------
# 第二步：下载数据（带本地缓存）
# ---------------------------------------------------------------------------
def download(force: bool = False) -> Path:
    """
    下载 GeoJSON 到 data/raw/admin/。

    为什么要缓存？
        这个文件 80KB，很小，但管道重跑时每次都请求一遍既慢又不礼貌。
        更现实的原因是：万一哪天接口改版或挂了，你手上至少还有一份数据。
        ——数据工作的第一条铁律：原始数据永远留档，不要直接覆盖。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if CACHE_FILE.exists() and not force:
        size_kb = CACHE_FILE.stat().st_size / 1024
        print(f"[2/5] 使用本地缓存：{CACHE_FILE.name}（{size_kb:.1f} KB）")
        return CACHE_FILE

    print(f"[2/5] 正在下载：{DATAV_URL}")
    # timeout 必须有。不设的话，网络卡住脚本会一直挂着，很难判断是死是活
    resp = requests.get(DATAV_URL, timeout=30)
    # raise_for_status 会在 4xx/5xx 时抛异常，避免把错误页当成数据存下来
    resp.raise_for_status()

    # 先写临时文件再改名：万一写到一半断电，不会留下一个半截的坏文件
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(CACHE_FILE)

    print(f"      下载完成：{len(resp.content) / 1024:.1f} KB -> {CACHE_FILE}")
    return CACHE_FILE


# ---------------------------------------------------------------------------
# 第三步：解析 GeoJSON
# ---------------------------------------------------------------------------
def parse_geojson(path: Path) -> list[dict]:
    """
    把 GeoJSON 的 features 数组读出来，整理成好入库的字典列表。

    GeoJSON 的层级是固定的：
        FeatureCollection          一个"要素集合"
          └─ features[]            每个要素 = 一块行政区域
               ├─ properties       属性表（名字、编码、中心点）
               └─ geometry         几何（MultiPolygon 之类的坐标）

    我们要的只有三样：名字 name、层级 level、几何 geometry。
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    if data.get("type") != "FeatureCollection":
        sys.exit(f"[错误] 期望 FeatureCollection，实际是 {data.get('type')}")

    rows = []
    for feature in data["features"]:
        props = feature.get("properties") or {}
        geom = feature.get("geometry")

        # 没有几何的要素直接跳过（DataV 偶尔会在边界争议区返回 null）
        if not geom:
            print(f"      [跳过] {props.get('name')} 没有几何数据")
            continue

        rows.append(
            {
                "name": props.get("name"),
                "level": "district",  # 这个接口返回的都是区县级
                # 几何要转成 JSON 字符串再交给 PostGIS 解析
                "geom_json": json.dumps(geom, ensure_ascii=False),
                "geom_type": geom["type"],
            }
        )

    print(f"[3/5] 解析完成：{len(rows)} 个要素")
    for r in rows[:3]:
        print(f"      · {r['name']}  ({r['geom_type']})")
    if len(rows) > 3:
        print(f"      · ... 共 {len(rows)} 个")
    return rows


# ---------------------------------------------------------------------------
# 第四步：写入 PostGIS
# ---------------------------------------------------------------------------
def load_to_postgis(rows: list[dict]) -> None:
    """
    清空 admin_boundary 表后，把这批数据整批写进去。

    两个 SQL 关键点：
      1. ST_GeomFromGeoJSON(%s)
         把一段 GeoJSON 格式的几何文本，解析成 PostGIS 的 geometry 对象。
         这是 PostGIS 提供的函数，不是 PostgreSQL 自带的。
      2. ST_SetSRID(..., 4326)
         给几何对象打上坐标系标签。4326 = WGS84 经纬度。
         不打标签的话，这个几何是"没有坐标系"的，后续 ST_Intersects
         之类跨表运算会报 "Operation on mixed SRID geometries"。

    为什么不逐条 INSERT？
        13 条无所谓，但等以后处理几万个雨量站、几十万条降雨记录时，
        逐条执行会慢得离谱（每条都要一次网络往返）。
        这里用 executemany，psycopg2 会合并成批量操作。
    """
    # 不传参数 = 自动读取 PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD 环境变量
    # （就是 load_env 刚刚塞进 os.environ 的那些）
    with psycopg2.connect() as conn:
        with conn.cursor() as cur:
            # TRUNCATE 比 DELETE 快，且会把自增 id 重置。
            # RESTART IDENTITY 让 id 从 1 重新开始，避免反复跑时 id 一直涨。
            cur.execute("TRUNCATE admin_boundary RESTART IDENTITY;")
            print("[4/5] 已清空 admin_boundary")

            sql = """
                INSERT INTO admin_boundary (name, level, geom)
                VALUES (
                    %s, %s,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))
                )
            """
            # ST_Multi 的作用：如果某个要素是单个 Polygon，把它包装成 MultiPolygon。
            # 我们的表字段类型声明成了 GEOMETRY(MultiPolygon, 4326)，
            # 塞 Polygon 进去会报类型不匹配，所以统一转一下最保险。
            cur.executemany(
                sql,
                [(r["name"], r["level"], r["geom_json"]) for r in rows],
            )

            # 再补一条"整个武汉市"的市级边界：
            # 把刚才录入的 13 个区县几何合并（ST_Union），就是全市轮廓。
            # 这样一张表里同时有市级和区级，level 字段用来区分。
            cur.execute(
                """
                INSERT INTO admin_boundary (name, level, geom)
                SELECT '武汉市', 'city', ST_Multi(ST_Union(geom))
                FROM admin_boundary
                WHERE level = 'district'
                """
            )

        # with 块结束时自动 commit。若中途抛异常则自动 rollback，
        # 不会留下"清空了但没写进去"的中间状态。
        conn.commit()

    print("[5/5] 写入完成")


# ---------------------------------------------------------------------------
# 第五步：回查验证
# ---------------------------------------------------------------------------
def verify() -> None:
    """
    入库后立刻查一遍，用数据本身证明成功——不要只看"没报错"。
    这是数据工程里非常重要的习惯：写进去 ≠ 写对了。
    """
    with psycopg2.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT level,
                   COUNT(*)                            AS 数量,
                   -- 注意这个 ::numeric 强制转换，不加就一定报错：
                   --   ST_Area(geom::geography) 返回 double precision（双精度浮点）
                   --   而 PostgreSQL 没有 ROUND(double precision, integer) 这个函数，
                   --   只有 ROUND(numeric, integer)。
                   --   浮点数存不下精确的十进制小数（0.1+0.2 会得到 0.30000000000000004），
                   --   对不精确的数"保留几位小数"没有意义，所以 PG 强制你先转 numeric。
                   ROUND((SUM(ST_Area(geom::geography)) / 1000000)::numeric, 1) AS 面积平方公里
            FROM admin_boundary
            GROUP BY level
            ORDER BY level DESC
            """
        )
        print()
        print("=" * 58)
        print(f"{'层级':<10}{'数量':>6}{'总面积(km²)':>18}")
        print("-" * 58)
        for level, cnt, area in cur.fetchall():
            print(f"{level:<10}{cnt:>6}{area:>18,.1f}")
        print("=" * 58)

        # 交叉验证：武汉全市实际面积约 8569 km²。
        # 如果算出来差得远，说明坐标系或几何有问题。
        cur.execute(
            """
            SELECT ROUND((ST_Area(geom::geography) / 1000000)::numeric, 1)
            FROM admin_boundary WHERE level = 'city'
            """
        )
        city_area = cur.fetchone()[0]
        print(f"\n武汉市总面积：{city_area:,.1f} km²（官方公布约 8569 km²）")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    force = "--force" in sys.argv

    load_env(ENV_FILE)
    path = download(force=force)
    rows = parse_geojson(path)
    load_to_postgis(rows)
    verify()

    print("\n下一步可以打开 IDEA 的 Database 面板看这张表，或者跑：")
    print("  SELECT name, level, ST_AsText(ST_Centroid(geom)) FROM admin_boundary;")


if __name__ == "__main__":
    main()
