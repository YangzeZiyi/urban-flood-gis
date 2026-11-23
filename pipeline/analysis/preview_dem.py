"""生成武汉 DEM 预览图：左高程分层设色，右山体阴影（晕渲）。"""
from pathlib import Path

import matplotlib
import numpy as np
import rasterio
from matplotlib.colors import LightSource
from rasterio.enums import Resampling

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 中文标签用雅黑，否则会变成方框
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

SRC = r"D:\wb\urban-flood-gis\data\processed\dem\wuhan_dem_cgcs2000.tif"
OUT = Path(r"D:\wb\urban-flood-gis\docs\images\wuhan_dem_preview.png")
OUT.parent.mkdir(parents=True, exist_ok=True)

with rasterio.open(SRC) as ds:
    # 输出宽度控制在 1400 像素，用 overview 抽样，不必读全分辨率
    scale = ds.width / 1400
    h = int(ds.height / scale)
    # 注意：indexes 传单个整数时，rasterio 会**自动降维**——
    # out_shape 给 (h, w) 就返回 (h, w)，给 (1, h, w) 也返回 (h, w)。
    # 这里绝不能写 [0]，否则会把第一行整条抽出来。
    data = ds.read(
        1,
        out_shape=(h, 1400),
        resampling=Resampling.average,
    ).astype(np.float64)
    nodata = ds.nodata
    res_m = abs(ds.transform.a) * scale

data[data == nodata] = np.nan

fig, axes = plt.subplots(1, 2, figsize=(15, 9))
fig.suptitle("武汉市 30 m DEM（Copernicus GLO-30，CGCS2000 3度带 / CM 114°E）",
             fontsize=15, y=0.96)

# ---- 左：高程分层设色 ----
# vmax 取 300 而不是 863：武汉 98% 的面积在 227 m 以下，
# 用最大值拉伸会让全图挤在色带最低端，看不出高低差异。
im = axes[0].imshow(data, cmap="terrain", vmin=0, vmax=300)
axes[0].set_title("高程（米）", fontsize=12)
cb = fig.colorbar(im, ax=axes[0], shrink=0.72, pad=0.02)
cb.set_label("海拔 m", fontsize=10)

# ---- 右：山体阴影 ----
ls = LightSource(azdeg=315, altdeg=45)  # 光源西北 45°，制图惯例
shade = ls.hillshade(np.nan_to_num(data, nan=0.0), vert_exag=3.0,
                     dx=res_m, dy=res_m)
# 平地（坡度 0）在 45° 光源下的理论亮度是 sin(45°) ≈ 0.707。
# 以它为基准做温和拉伸：平地保持中灰，坡面才会同时出现亮面和暗面。
# 如果直接 (shade-0.55)*2.2 这种硬拉伸，平原会整片爆白、山地死黑。
shade = np.clip((shade - 0.707) * 1.6 + 0.62, 0, 1)
axes[1].imshow(shade, cmap="gray", vmin=0, vmax=1)
axes[1].set_title("山体阴影（西北光源 45°，垂直夸张 ×3）", fontsize=12)

for ax in axes:
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("数据：Copernicus DEM GLO-30 / ESA", fontsize=8, color="#666")

fig.text(0.5, 0.03,
         "北部黄陂山地最高 863.6 m，中部沿江平原 20~30 m，南部江夏丘陵缓坡 — "
         "地形格局与武汉“北山南丘、中间低平”的实际一致",
         ha="center", fontsize=10, color="#333")

fig.tight_layout(rect=(0, 0.05, 1, 0.94))
fig.savefig(OUT, dpi=130, bbox_inches="tight")
print(f"已生成：{OUT}")
print(f"图像尺寸：1400 x {h}，格网 {res_m:.0f} m")
