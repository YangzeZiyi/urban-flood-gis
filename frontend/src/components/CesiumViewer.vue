<script setup>
/**
 * CesiumViewer.vue —— 三维地球组件
 * ---------------------------------------------------------------------------
 * 这是整个项目里"最值钱"的一个文件：把 Cesium 的三维球装进 Vue 组件里。
 * 学会它，后面加载易涝区、雨量站、内涝水深，都是照着这个骨架加 Entity。
 *
 * <script setup> 是 Vue 3 的语法糖：里面定义的变量、函数、导入的组件，
 * 模板里都能直接用，不用 return、不用注册。
 */
import { onMounted, onBeforeUnmount, ref } from 'vue'
import * as Cesium from 'cesium'

// ref(值) 创建一个"响应式引用"。DOM 元素绑定到它之后，
// 可以在 JS 里用 container.value 拿到那个真实 div
const container = ref(null)

// 存 viewer 实例。注意不能用 ref 包：一个三维场景对象上千个属性，
// 让 Vue 去监听它会卡死，用普通变量即可
let viewer = null

// 把相机飞到武汉（经度, 纬度, 高度米）
const HOME = { lon: 114.305, lat: 30.593, height: 26000 }

// ---------------- 贴图相关状态 ----------------
// 注意分工：overlayLayer 是 Cesium 对象，不能塞进 ref（Vue 代理会拖慢它），
// 而图例内容要给模板用，必须放进 ref 才会驱动界面更新
let overlayLayer = null
const legend = ref([])        // [{ name, lo, hi, color }]，由 JSON 提供
const legendFoot = ref('')    // 图例底部那行小字
const overlayOn = ref(true)   // 图例上的勾选框

// 因子英文键 → 中文名，只为了图例底部那行字好读
const CN = { elev: '高程', slope: '坡度', fill: '填洼', acc: '汇流' }

/**
 * 勾选框变化时调用：显隐整张贴图。
 * 为什么不用 computed 自动同步？因为 overlayLayer 是普通变量，
 * Vue 看不见它的变化，只能靠事件手动推一下。
 */
function toggleOverlay() {
  if (overlayLayer) overlayLayer.show = overlayOn.value
}

// ---------------- 天地图 key ----------------
// import.meta.env 是 Vite 注入环境变量的方式，值来自 frontend/.env
// 只有 VITE_ 开头的变量才会被注入，避免把服务器密钥误打到前端
const TDT_KEY = import.meta.env.VITE_TDT_KEY

if (!TDT_KEY) {
  // 没配 key 时地球会是空白的，很容易误以为是代码坏了，这里给一条明确提示
  console.error('[天地图] 没有读到 VITE_TDT_KEY，请在 frontend/.env 里配置后重启 dev server')
}

/**
 * 造一个天地图 WMTS 图层。
 * 天地图把"底图"和"注记（地名、路名）"拆成两层，需要分别加载、叠加显示。
 *
 * @param {string} layer  LAYER 参数：img=影像 / cia=影像注记 / vec=矢量 / cva=矢量注记
 * @param {string} path   服务路径：img_w / cia_w / vec_w / cva_w（w 表示 Web 墨卡托投影）
 */
function tiandituLayer(layer, path) {
  const provider = new Cesium.WebMapTileServiceImageryProvider({
    // 这个 URL 里混着两种括号，别搞混：
    //   {s}         -> Cesium 用 subdomains 里的值替换，实现多域名分流
    //   {TileMatrix}-> Cesium 替换成当前层级 z
    //   {TileRow}   -> 行号 y     {TileCol} -> 列号 x
    // 注意大小写：Cesium 认的是首字母大写的 TileMatrix/TileRow/TileCol
    url:
      `https://t{s}.tianditu.gov.cn/${path}/wmts` +
      `?service=wmts&request=GetTile&version=1.0.0` +
      `&LAYER=${layer}&tileMatrixSet=w` +
      `&TileMatrix={TileMatrix}&TileRow={TileRow}&TileCol={TileCol}` +
      `&style=default&format=tiles&tk=${TDT_KEY}`,
    layer,
    style: 'default',
    format: 'tiles',
    tileMatrixSetID: 'w',   // w = Web 墨卡托，和 Cesium 默认的切片方案一致
    subdomains: ['0', '1', '2', '3', '4', '5', '6', '7'], // 8 个域名分流，加载更快
    maximumLevel: 18,       // 天地图影像最深 18 级，写大了会满屏 404
  })

  // 瓦片加载失败排查：天地图最常见的失败是"key 没绑当前域名"，
  // 浏览器只会静默显示空白，很难查。这里把原因直接打到控制台（F12 可见）
  provider.errorEvent.addEventListener((err) => {
    console.error(
      `[天地图] 瓦片加载失败 ${path} level=${err.level} x=${err.x} y=${err.y}`,
      err.error?.message || err.error,
    )
  })

  return provider
}

onMounted(async () => {
  // ---------------- 构造 Viewer ----------------
  viewer = new Cesium.Viewer(container.value, {
    // 底图：天地图卫星影像。
    // 天地图是 CGCS2000 坐标系，和我们库里的 WGS84 数据差异在厘米级，
    // 易涝区多边形能严丝合缝压在地物上——这是换掉高德（GCJ-02，偏移几百米）的原因
    baseLayer: new Cesium.ImageryLayer(tiandituLayer('img', 'img_w')),

    // 下面这些都是"关掉默认控件"，默认全开会让界面很花
    animation: false,        // 左下角时间播放器
    timeline: false,         // 底部时间轴（后面做时间轴回放会重新打开）
    baseLayerPicker: false,  // 右上角底图切换器（在线底图需要它，但会连 Ion）
    geocoder: false,         // 右上角搜索框（默认走 Bing，国内用不了）
    navigationHelpButton: false, // 右上角帮助按钮
    homeButton: true,        // 保留"回到初始视角"，好用
    sceneModePicker: true,   // 保留 3D/2D/哥伦布视图切换，演示时很直观
    fullscreenButton: false,
    infoBox: false,          // 点击要素弹出的信息面板（我们用自定义的）
    selectionIndicator: false,
  })

  // ---------------- 叠加易涝指数贴图（M3 的成果） ----------------
  // 这是"从数据到屏幕"的最后一公里：一张 PNG + 一个经纬度矩形，就这么多。
  //
  // 为什么不用 GeoJSON 多边形？
  //   指数有 2268 万个像元，转成多边形就是几十万个矩形，浏览器必卡死。
  //   用贴图，GPU 只画一个四边形，代价几乎为零。
  //
  // 三点必须知道：
  //   1. 影像图层按 add 的先后叠，index 0 在最底下。
  //      所以这里的顺序是：底图(0) → 指数贴图(1) → 注记(2)。
  //      注记层一定放在最后 add，否则地名会被这张半透明图糊住。
  //   2. 贴图是"矩形 + 整幅图"一次铺满，缩放超过约 1:10 万会开始糊
  //      （原图只有 1110x1291 像素）。要看清得切瓦片，那是后面的事。
  //   3. bbox 和分级阈值全部从 JSON 读，前端一个数字都不手抄——
  //      改脚本，前端自动跟着变。这叫单一数据源（single source of truth）。
  try {
    const metaResp = await fetch('/overlay/wuhan_flood_index.json')
    if (!metaResp.ok) throw new Error(`HTTP ${metaResp.status}`)
    const meta = await metaResp.json()

    // bbox 的顺序是（西, 南, 东, 北），单位是度，和 fromDegrees 的参数完全对应。
    // 这个顺序值得记一下：很多数据集的 bbox 写成 [minX, minY, maxX, maxY]，
    // 其实是同一套；但如果谁给了 [西, 东, 南, 北]，图会跑到海里，
    // 或者直接报 "south must be less than north"——报错算幸运，静默错位才要命。
    const [w, s, e, n] = meta.bbox_wgs84
    const rectangle = Cesium.Rectangle.fromDegrees(w, s, e, n)

    const provider = new Cesium.SingleTileImageryProvider({
      url: `/overlay/${meta.file}`,
      rectangle,
      // 这两个参数在 1.147 里**是必填的**，虽然官方文档标着"可选"。
      // 源码里是 Check.typeOf.number("options.tileWidth", ...)，不传就抛
      // DeveloperError: Expected options.tileWidth to be typeof number。
      // 而且必须和 PNG 的真实像素尺寸一致，否则贴图会被拉变形。
      // 我们的 JSON 里正好记了 size_px，直接取用，不再手抄。
      tileWidth: meta.size_px[0],
      tileHeight: meta.size_px[1],
    })

    overlayLayer = viewer.imageryLayers.addImageryProvider(provider)

    // ================================================================
    // TODO ★ 贴图的不透明度，填 0~1 之间的数 ★
    //
    //   1.00 —— 完全按 PNG 自己的透明度铺。红色区域会盖住底图，
    //           看不出"这个高风险区到底压在江滩上还是堤后"。
    //   0.85 —— 还能看清红色，同时透出底图的江、路、绿地。
    //   0.60 —— 底图很清楚，但红色变淡，远处几乎看不出来。
    //
    //   这不是语法题，是取舍：你到底想让观众先看风险，还是先看地形？
    //   想清楚了再填，填完在页面上滚轮放大缩小感受一下。
    // ================================================================
    overlayLayer.alpha = 0.85        // <- 改这一行

    // 图例内容也从 JSON 来
    legend.value = meta.levels
    legendFoot.value =
      `陆地 ${meta.land_km2.toLocaleString()} km² · ` +
      `权重 ` +
      Object.entries(meta.weights)
        .map(([k, v]) => `${CN[k] ?? k}${v.toFixed(2)}`)
        .join(' / ')

    console.log(`[贴图] ${meta.file} 已铺在 ${rectangle.west} ~ ${rectangle.east}`, meta.bbox_wgs84)
  } catch (err) {
    // 贴图失败不该让整页白屏：底图、区界、易涝区都还能用，只是少了指数
    console.error('[贴图] 加载失败，检查 frontend/public/overlay/ 下的 png 和 json', err)
  }

  // ---------------- 叠加注记层 ----------------
  // 只有影像没有地名，看不出这里是哪儿。再叠一层"影像注记"，
  // 顺序很重要：后 add 的盖在上面，所以注记要放在底图之后
  viewer.imageryLayers.addImageryProvider(tiandituLayer('cia', 'cia_w'))

  // ---------------- 场景微调 ----------------
  // 关掉抗锯齿以外的花活，先保证帧率
  viewer.scene.globe.enableLighting = false // 打开会有昼夜分割线，看数据时反而碍事
  viewer.scene.fog.enabled = false          // 远处雾化，关掉后地图边缘不发灰
  // 分辨率缩放：1.0 是原生。笔记本核显可以调到 0.8 换流畅度
  viewer.resolutionScale = 1.0

  // ---------------- 飞到武汉 ----------------
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(HOME.lon, HOME.lat, HOME.height),
    orientation: {
      heading: 0,                 // 朝向正北（弧度）
      pitch: Cesium.Math.toRadians(-90), // -90° = 垂直俯视地面
      roll: 0,
    },
  })

  // ---------------- 放一个标记，证明 Entity 能画出来 ----------------
  // Entity 是 Cesium 的"业务对象"：一个点、一条线、一个面、一个模型。
  // 以后每个易涝区就是一个 Entity，只是 position 换成 polygon。
  viewer.entities.add({
    id: 'demo-wuhan',
    name: '武汉市',
    position: Cesium.Cartesian3.fromDegrees(HOME.lon, HOME.lat, 0),
    point: {
      pixelSize: 10,
      color: Cesium.Color.fromCssColorString('#3fa9f5'),
      outlineColor: Cesium.Color.WHITE,
      outlineWidth: 2,
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND, // 贴地，不会被地形挡住
    },
    label: {
      text: '武汉',
      font: '600 15px "Microsoft YaHei", sans-serif',
      fillColor: Cesium.Color.WHITE,
      outlineColor: Cesium.Color.BLACK,
      outlineWidth: 3,
      style: Cesium.LabelStyle.FILL_AND_OUTLINE,
      pixelOffset: new Cesium.Cartesian2(0, -26), // 往上挪一点，别压住点
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
    },
  })

  // ---------------- 从后端加载行政边界 ----------------
  // ⚠️ 这里有个 Cesium 的硬限制（我们实际踩过）：
  //    贴地的多边形（clampToGround: true）**不支持 outline 描边**。
  //    Cesium 源码里创建描边的那行是 `if (!onTerrain && ...)`，
  //    只要多边形贴地，描边分支直接被跳过。
  //    所以「fill 设透明 + stroke 上色」的结果是：屏幕上什么都不出现。
  //
  //    正确做法：把每个多边形的每一圈边界取出来，画成「贴地折线」。
  //    折线走 GroundPolylinePrimitive，既贴地，又能用像素控制线宽。
  //
  // ?level=district 只取 13 个区县。不传会连市级轮廓一起返回，
  // 市级轮廓和区县外沿完全重合，会画出双重线，反而难看。
  const resp = await fetch('/api/admin?level=district')
  if (!resp.ok) throw new Error(`边界接口返回 HTTP ${resp.status}`)
  const adminGeoJson = await resp.json()

  const lineColor = Cesium.Color.fromCssColorString('#7fd4ff').withAlpha(0.95)
  const instances = []
  // 顺便算出整体经纬度范围，等下用它精确框住相机
  let west = 180
  let south = 90
  let east = -180
  let north = -90

  for (const feature of adminGeoJson.features) {
    const g = feature.geometry
    if (!g) continue
    // Polygon 和 MultiPolygon 统一成「多边形数组」，省得分两套逻辑
    const polygons = g.type === 'MultiPolygon' ? g.coordinates : [g.coordinates]

    for (const polygon of polygons) {
      // 一个多边形可能有多个环：第 1 圈是外轮廓，后面的是「洞」（飞地、湖泊）
      for (const ring of polygon) {
        // ring 是 [[lon, lat], ...]，Cesium 要扁平的 [lon, lat, lon, lat, ...]
        const flat = []
        for (const [lon, lat] of ring) {
          flat.push(lon, lat)
          if (lon < west) west = lon
          if (lon > east) east = lon
          if (lat < south) south = lat
          if (lat > north) north = lat
        }
        instances.push(
          new Cesium.GeometryInstance({
            geometry: new Cesium.GroundPolylineGeometry({
              positions: Cesium.Cartesian3.fromDegreesArray(flat),
              width: 2, // 屏幕像素宽，缩放时线不会变粗变细
            }),
            attributes: {
              color: Cesium.ColorGeometryInstanceAttribute.fromColor(lineColor),
            },
          }),
        )
      }
    }
  }

  // 所有线合成一个 primitive 一次性提交 GPU。
  // 20 多条线如果各建一个 primitive，绘制调用会翻倍，这里只花 1 次。
  //
  // translucent: true 不能省：我们的线色 alpha 是 0.95（半透明），
  // 不开这个开关，Cesium 会按"不透明"渲染，混合结果可能发黑。
  viewer.scene.primitives.add(
    new Cesium.GroundPolylinePrimitive({
      geometryInstances: instances,
      appearance: new Cesium.PolylineColorAppearance({ translucent: true }),
    }),
  )
  console.log(
    `[边界] 已绘制 ${instances.length} 条边界线，覆盖 ${adminGeoJson.features.length} 个区县`,
  )

  // ---------------- 从后端加载易涝区 ----------------
  const zones = await Cesium.GeoJsonDataSource.load('/api/zones', { clampToGround: true })
  viewer.dataSources.add(zones)
  console.log(`[易涝区] 已加载 ${zones.entities.values.length} 条`)

  // ---------------- 相机飞到全市范围 ----------------
  // 不用 flyTo(数据源)：它按包围球算距离、还带俯角，实际会飞得偏远
  // （你上一张截图视野宽到了荆门、黄石，就是这个原因）。
  // 用 Rectangle 最准：相机自动调高度，让这个矩形刚好铺满视野。
  const PAD = 0.05 // 四周各留 5% 余量，免得区界贴着屏幕边
  await viewer.camera.flyTo({
    destination: Cesium.Rectangle.fromDegrees(west - PAD, south - PAD, east + PAD, north + PAD),
    duration: 1.5,
  })

  // 风险等级 → 颜色：1低 2中 3高 4极高
  const COLORS = { 1: '#2f81f7', 2: '#d29922', 3: '#f0883e', 4: '#f85149' }

  for (const entity of zones.entities.values) {
    // properties 里的每个值都是 Cesium 的 Property 对象，要用 getValue() 取值
    const level = entity.properties?.riskLevel?.getValue() ?? 1
    const color = Cesium.Color.fromCssColorString(COLORS[level] ?? COLORS[1])

    if (entity.polygon) {
      entity.polygon.material = color.withAlpha(0.45) // 半透明，能看见底图
      entity.polygon.outline = true
      entity.polygon.outlineColor = color
      entity.polygon.outlineWidth = 2
    }
  }
})

// 组件销毁时一定要 destroy，否则 GPU 显存不释放，热更新几次浏览器就崩了。
// 这是 Cesium + Vue 最容易踩的坑。
onBeforeUnmount(() => {
  if (viewer && !viewer.isDestroyed()) {
    viewer.destroy()
    viewer = null
  }
})

/**
 * 底图备选方案
 * ---------------------------------------------------------------------------
 * 当前用的是天地图（CGCS2000，与我们的 WGS84 数据厘米级吻合），key 存在 .env。
 *
 * 方案 A：Cesium Ion（官方，需注册免费账号拿 token，国内访问不稳）
 *   Cesium.Ion.defaultAccessToken = '你的token'
 *   baseLayer: Cesium.ImageryLayer.fromProviderAsync(
 *     Cesium.IonImageryProvider.fromAssetId(2))   // 2 = Bing 影像
 *
 * 方案 B：高德卫星图（无需 key，但是 GCJ-02 火星坐标，
 *   和 WGS84 数据有 300~500 米偏移，只适合临时看效果，不能用于正式成果）
 *   new Cesium.ImageryLayer(new Cesium.UrlTemplateImageryProvider({
 *     url: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
 *     subdomains: ['1','2','3','4'], maximumLevel: 18,
 *   }))
 *
 * 方案 C：Cesium 自带离线底图 NaturalEarthII（无需联网，但最高只有 2 级瓦片，
 *   放大后是一片纯色，仅适合断网演示）
 *   baseLayer: Cesium.ImageryLayer.fromProviderAsync(
 *     Promise.resolve(new Cesium.TileMapServiceImageryProvider({
 *       url: Cesium.buildModuleUrl('Assets/Textures/NaturalEarthII') })))
 */
</script>

<template>
  <!-- ref="container" 把这个 div 的 DOM 交给上面的 container 变量 -->
  <div ref="container" class="cesium-container">
    <!--
      图例浮层：和 App.vue 里那块状态面板同一套配色变量（--panel 等）。
      为什么要放在这个组件里而不是 App.vue？
      因为它只服务于"贴图"这一件事，跟着组件走，删掉组件图例自然消失，不会留孤儿。
    -->
    <aside v-if="legend.length" class="legend">
      <div class="legend-head">
        <span>易涝风险指数</span>
        <label class="switch">
          <input v-model="overlayOn" type="checkbox" @change="toggleOverlay" />
          <span>贴图</span>
        </label>
      </div>

      <ul>
        <li v-for="lv in legend" :key="lv.name">
          <i :style="{ background: lv.color }"></i>
          <span>{{ lv.name }}</span>
          <b>{{ lv.lo.toFixed(2) }}~{{ lv.hi > 1 ? '1.00' : lv.hi.toFixed(2) }}</b>
        </li>
      </ul>

      <p class="legend-foot">{{ legendFoot }}</p>
    </aside>
  </div>
</template>

<style scoped>
.cesium-container {
  position: relative;   /* 给图例做定位基准 */
  width: 100%;
  height: 100%;
  background: #000;
  overflow: hidden;
}

/* ---------- 图例 ---------- */
.legend {
  position: absolute;
  right: 16px;
  bottom: 34px;          /* 让开 Cesium 左下角的版权条 */
  z-index: 20;           /* 必须压过 canvas */
  width: 208px;
  padding: 12px 14px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  backdrop-filter: blur(10px);
  font-size: 12px;
  color: var(--text);
  user-select: none;
}

.legend-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 8px;
}

.switch {
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 400;
  font-size: 11px;
  color: var(--text-dim);
  cursor: pointer;
}

.switch input {
  accent-color: var(--accent);
}

.legend ul {
  list-style: none;
}

.legend li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
}

/* 色块 */
.legend li i {
  width: 12px;
  height: 12px;
  border-radius: 3px;
  flex: none;
}

.legend li span {
  flex: 1;
}

.legend li b {
  font-weight: 400;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

.legend-foot {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed rgba(255, 255, 255, 0.08);
  font-size: 10px;
  line-height: 1.5;
  color: var(--text-dim);
}

/* Cesium 自带的版权信息默认在左下角，字号偏小，这里统一调一下 */
:deep(.cesium-viewer-bottom) {
  left: 12px;
  bottom: 8px;
}
</style>
