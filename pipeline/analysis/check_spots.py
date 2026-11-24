"""
改判据之后的体检：湖心该是水，渍水点该留在地上
=====================================================================
用法
----
    python pipeline/analysis/check_spots.py

读的是**最终产物** `wuhan_flood_index_grid.bin`（Cesium 贴图和点击查询用的
就是它），不是中间量 `wuhan_flood_index.npy`。

为什么强调这一点：
    `.npy` 存的是完整指数，水体和市域外也有数值，只是统计和出图时被 mask 掉了。
    拿 `.npy` 判"这里是不是水"会永远判成陆地——这个坑我们刚踩过一次。
    判据的最终裁决体现在 `.bin` 的 index 通道：水体写 NaN。

判读规则
--------
- 输出 `水体`  → index 是 NaN，被掩膜剔掉了
- 输出 `低/较低/中/高/极高` → 参与了评价，落在哪一档

湖心必须是"水体"，否则判据没抓到；
渍水点必须是等级，如果变成"水体"，说明判据误伤了真实易涝区。
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parents[2]
GRID_DIR = ROOT / "data" / "processed" / "dem"

# (名字, 经度, 纬度, 期望)。期望只用于打印 OK/FAIL，不参与判断。
#
# 注意：这里的坐标必须是**陆地上的具体位置**，不能是"某某湖片区"这种模糊说法。
# 最初写的「南湖（洪山）114.335,30.485」和「汤逊湖（江夏）114.380,30.430」，
# 查了填深才发现它们本身就落在湖面上（fill 1.9~5.8m、坡度 0.000）——
# 那不是判据误伤陆地，是坐标选在了水里。验收点选错，会把正确的算法判成 bug。
SPOTS = [
    ("东湖·湖心",      114.3650, 30.5600, "水"),
    ("南湖·湖心",      114.3350, 30.4900, "水"),
    ("沙湖·湖心",      114.3180, 30.5720, "水"),
    ("汤逊湖·湖心",    114.3400, 30.4200, "水"),
    ("长江·江心",      114.2960, 30.5770, "水"),
    ("南湖·雅园",      114.3220, 30.4880, "陆"),
    ("汤逊湖·文化大道", 114.3700, 30.4000, "陆"),
    ("后湖（江岸）",   114.3050, 30.6500, "陆"),
    ("四新（汉阳）",   114.2400, 30.5100, "陆"),
    ("青山建一",       114.3900, 30.6200, "陆"),
    ("武昌站",         114.3170, 30.5290, "陆"),
]


def main() -> None:
    meta = json.loads((GRID_DIR / "wuhan_flood_index_grid.json")
                      .read_text(encoding="utf-8"))
    width, height = meta["width"], meta["height"]
    west, south, east, north = meta["bbox_wgs84"]
    levels = meta["levels"]
    raw = (GRID_DIR / meta["file"]).read_bytes()
    cells = np.frombuffer(raw, dtype="<f4").reshape(height, width, 4)

    print(f"栅格 {width} x {height}，{meta['res_deg'][0] * 3600:.0f} 角秒/格"
          f"（约 {meta['res_deg'][0] * 111320 * np.cos(np.radians(30.6)):.0f} m）\n")
    print(f"{'点位':<16}{'期望':<5}{'指数':>8}  {'判定':<6}{'结果'}")
    print("-" * 50)

    bad = 0
    for name, lon, lat, expect in SPOTS:
        col = int((lon - west) / (east - west) * width)
        row = int((north - lat) / (north - south) * height)
        col = max(0, min(width - 1, col))
        row = max(0, min(height - 1, row))
        v = float(cells[row, col, 0])

        if np.isnan(v):
            got, tag = "水", "水体"
        else:
            got = "陆"
            tag = next((lv["name"] for lv in levels
                        if lv["lo"] <= v < lv["hi"]), "?")
        ok = "OK" if got == expect else "FAIL"
        bad += got != expect
        print(f"{name:<16}{expect:<5}{v:>8.3f}  {tag:<6}{ok}")

    print("-" * 50)
    print("全部符合预期" if bad == 0 else f"{bad} 个点位不符合预期")


if __name__ == "__main__":
    main()
