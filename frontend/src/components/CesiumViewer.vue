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
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
const overlayOn = ref(true)   // 图例上的勾选框（= 用户"此刻"看到的静态贴图状态）
let staticFoot = ''           // 静态图例底部那行原文，退出时间轴时要原样还回去
let noteLayer = null          // 影像注记层（地名），必须永远压在最上面

// 时间轴模式会把静态贴图强制关掉，但那是"临时让位"，不是用户改主意了。
// 所以进入前先把用户的选择抄一份，退出时原样还回去——
// 否则"我明明关了贴图，进一次时间轴出来它又自己开了"，用户会以为程序在乱动他的设置。
let overlayBeforeTimeline = true

// ---------------- 动态风险时间轴（M6） ----------------
// 2020 年汛期（5/1~9/30）是 153 帧，不能再像 7 帧那样"一次全建好只切 show"：
//   - 每帧贴图 1656×1671 解码后约 11 MB，153 张就是 1.6 GB 显存，
//     浏览器会直接丢掉 WebGL 上下文，页面变成一片灰；
//   - 首屏还要先下 36 MB，用户打开页面第一秒就是白屏。
// 所以改成"按需建层 + 最近窗口缓存"：只留当前帧前后各 1 帧（最多 3 个图层，
// 约 33 MB），滑到哪建到哪。代价是快速拖动时偶尔要等一次贴图上传，
// 换来的是 153 帧和 7 帧一样能开得起来。
const timeline = ref(null)      // manifest，null = 还没加载完或加载失败
const frameIdx = ref(0)         // 当前帧下标
const playing = ref(false)      // 是否在自动播放
const timelineOn = ref(false)   // 时间轴模式开关（打开时静态贴图让位）
const frameLoading = ref(false) // 当前帧贴图还在路上，界面上给个"载入中"
let frameCache = new Map()      // 下标 -> Cesium.ImageryLayer，只留最近几帧
let slideTimer = null           // 拖动防抖的 setTimeout 句柄
let playTimer = null            // setInterval 句柄，退出/卸载时必须清

const CACHE_RADIUS = 1          // 当前帧前后各留几帧（1 => 最多 3 个图层）
const PLAY_INTERVAL = 700       // 自动播放每帧毫秒数。153 帧 × 0.7 s ≈ 1.8 分钟一轮

// 时间轴模式下把图例底部那句话换掉：它解释的是"为什么 7 张图的颜色能直接比大小"
const TIMELINE_FOOT = '动态风险与静态图同一套分级阈值，红区面积可直接跨日期比较'

// 点击查询的结果卡片。null = 不显示
// 结构：{ status, lon, lat, index, level, color, elev, slope, acc, message, x, y }
const pick = ref(null)
let pickEntity = null         // 点击位置的标记点
let clickHandler = null       // Cesium 事件句柄，销毁时要一起释放

// 因子英文键 → 中文名，只为了图例底部那行字好读
const CN = { elev: '高程', slope: '坡度', fill: '填洼', acc: '汇流' }

// ---------------- 框选统计 ----------------
// selectMode：是否处于"拖框"状态。开启时左键不再转地球，而是画矩形
const selectMode = ref(false)
const stats = ref(null)       // 统计结果，null = 不显示
let rectEntity = null          // 图上那个黄色矩形
let dragStart = null           // 拖拽起点（经纬度），null = 当前没在拖
let suppressClickUntil = 0     // 拖完会顺带触发一次 LEFT_CLICK，用它屏蔽掉

/** 数字格式化：后端在"没有有效像元"时会返回 null，别让它显示成 NaN */
function num(v, digits = 2) {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : '—'
}

/**
 * 把选框从地球上摘掉。
 * 三个地方会调用：关卡片、重新进入框选、新一次按下鼠标。
 * 注意 viewer 可能已经被销毁（热更新时先跑卸载钩子），所以要先判活。
 */
function clearRect() {
  if (rectEntity && viewer && !viewer.isDestroyed()) {
    viewer.entities.remove(rectEntity)
  }
  rectEntity = null
}

// 卡片一关，就说明这次框选结束了，框也该跟着走。
// 不这么做的话，每关一次卡片就在地图上留一个半透明方块：
// 黄色 0.25 压在红色指数上会变成橙红色，看起来像"这片区域特别危险"，
// 而不是"我用鼠标框过这里"——这是最坑的一类 bug，因为图还能看，只是看错了。
watch(stats, (v) => {
  if (!v) clearRect()
})

/** 进入/退出框选模式 */
function toggleSelect() {
  selectMode.value = !selectMode.value
  pick.value = null
  if (selectMode.value) {
    // 重新进入框选：上一轮的卡片和框都过期了，先擦干净再让人画新的
    stats.value = null
    clearRect()
  } else {
    // 中途退出：万一鼠标还按着，把相机控制还回去，否则地图会"卡死"
    dragStart = null
    if (viewer) viewer.scene.screenSpaceCameraController.enableInputs = true
  }
}

/** Esc 退出框选。挂在 window 上而不是画布上，因为焦点可能在按钮上 */
function onKeyDown(e) {
  if (e.key === 'Escape' && selectMode.value) {
    toggleSelect()
  }
}

/**
 * 勾选框变化时调用：显隐整张贴图。
 * 为什么不用 computed 自动同步？因为 overlayLayer 是普通变量，
 * Vue 看不见它的变化，只能靠事件手动推一下。
 *
 * 为什么时间轴模式下直接 return？
 *   模板上已经给了 :disabled，用户点不动；这里再拦一道是为了防"程序内调用"
 *   （比如将来加个键盘快捷键、或者 console 里手动改）。一旦放行，静态图会被
 *   show=true 垫到动态帧底下，而动态层 alpha 只有 0.85，最终颜色会变成
 *   0.85×动态 + 0.15×静态——画面上那个颜色不再属于任何一天的数据。
 */
function toggleOverlay(e) {
  if (timelineOn.value) {
    // 把 DOM 的勾选状态直接按回去。为什么不能只改 overlayOn？
    //   v-model 会先在同一个事件里把模型置 true，我们再置回 false，
    //   两次赋值发生在同一轮更新里，Vue 认为"值没变过"就不重渲染，
    //   屏幕上会留下一个勾上的、但其实没生效的勾选框。
    //   这种"显示假状态"比"点不动"更糟，所以 DOM 手动兜一下。
    if (e && e.target) e.target.checked = false
    overlayOn.value = false
    return
  }
  if (overlayLayer) overlayLayer.show = overlayOn.value
}

// ---------------- 时间轴：按需建层 / 切帧 / 播放 / 开关 ----------------
/**
 * 拿到第 i 帧的图层，没有就现建一个。
 *
 * 为什么清缓存时用 remove(layer, true) 而不是留着？
 *   第二个参数 true = 连贴图一起销毁。不传的话图层对象摘掉了，
 *   但 GPU 里那 11 MB 纹理还在，拖完 153 帧等于把整个汛期都留在显存里，
 *   白做懒加载。
 */
function ensureLayer(i) {
  const hit = frameCache.get(i)
  if (hit) return hit

  const tl = timeline.value
  const [tw, ts, te, tn] = tl.bbox_wgs84
  const f = tl.frames[i]
  const provider = new Cesium.SingleTileImageryProvider({
    url: `/risk/${f.img}`,
    rectangle: Cesium.Rectangle.fromDegrees(tw, ts, te, tn),
    // tileWidth/tileHeight 必须等于贴图真实像素，否则被拉伸变形。
    // 这两个值来自 manifest，不在这里写死——改了导出分辨率前端自动跟着变。
    tileWidth: tl.width,
    tileHeight: tl.height,
  })
  const layer = viewer.imageryLayers.addImageryProvider(provider)
  layer.alpha = 0.85         // 和静态贴图同一个不透明度，切模式时观感才一致
  layer.show = false
  frameCache.set(i, layer)

  // 新 add 的影像层一定压在注记层之上，必须再把注记抬回去，
  // 否则建一帧糊一次地名。7 帧时代这一步只在全部建完时做一次，现在每帧都要做。
  if (noteLayer) viewer.imageryLayers.raiseToTop(noteLayer)
  return layer
}

/** 丢掉离当前帧超出一个窗口的图层，把显存还回去 */
function evict(keepIdx) {
  for (const [i, layer] of frameCache) {
    if (Math.abs(i - keepIdx) <= CACHE_RADIUS) continue
    viewer.imageryLayers.remove(layer, true)
    frameCache.delete(i)
  }
}

/**
 * 真正把第 i 帧显示出来：建层 → 只 show 它 → 清远处 → 预取邻居。
 *
 * 为什么只预取不解码成图层？
 *   new Image() 只把文件下进 HTTP 缓存，不占显存；下一格要显示时
 *   SingleTileImageryProvider 直接命中缓存，省掉一次网络往返。
 *   要是预取也建图层，就等于变相把懒加载又写回了全量加载。
 */
function showFrame(i) {
  if (!timeline.value) return
  const n = timeline.value.frames.length
  frameIdx.value = Math.max(0, Math.min(n - 1, i))
  const idx = frameIdx.value

  const layer = ensureLayer(idx)
  for (const [k, l] of frameCache) l.show = timelineOn.value && k === idx
  layer.show = timelineOn.value
  evict(idx)

  // 贴图还在解码时给个提示。
  //
  // 怎么判断"贴图就绪"？不能问 Cesium：
  //   1.147 的 ImageryProvider 把 ready / readyPromise 全拆了
  //   （实测 `'ready' in provider === false`，两种都是 undefined），
  //   改成只能通过异步的 fromUrl 构造。我们这里是同步构造，
  //   拿不到任何就绪信号，写 provider.ready 恒为 undefined，
  //   界面上就会永远挂着"载入中…"——图其实早画出来了，提示在骗人。
  //
  // 所以自己 load 一次同一张图来判断：浏览器按 URL 去重，
  // Cesium 内部发的也是这个 URL，不会多下载一份，只是多一个解码回调。
  // 用 idx 做令牌：快速拖动时先后建了好几帧，只有最后落到的那一帧
  // 才有资格关提示，否则会被先完成的旧帧提前清掉。
  const probe = new Image()
  probe.onload = () => { if (frameIdx.value === idx) frameLoading.value = false }
  probe.onerror = () => {
    // 图挂了也别把提示挂天上，否则用户以为一直在加载
    if (frameIdx.value === idx) frameLoading.value = false
    console.warn(`[时间轴] ${timeline.value.frames[idx].img} 加载失败`)
  }
  probe.src = `/risk/${timeline.value.frames[idx].img}`
  frameLoading.value = !probe.complete

  for (const d of [-1, 1]) {
    const j = idx + d
    if (j >= 0 && j < n && !frameCache.has(j)) {
      const im = new Image()
      im.src = `/risk/${timeline.value.frames[j].img}`
    }
  }
}

/** 停掉自动播放。播放器、退出时间轴、组件卸载三个地方都要调它 */
function stopPlay() {
  playing.value = false
  if (playTimer) {
    clearInterval(playTimer)
    playTimer = null
  }
}

/** ▶ / ⏸：每帧 700 ms，走到最后回到第一帧循环，方便反复看雨怎么蔓延 */
function togglePlay() {
  if (playing.value) {
    stopPlay()
    return
  }
  if (!timeline.value) return
  playing.value = true
  playTimer = setInterval(() => {
    showFrame((frameIdx.value + 1) % timeline.value.frames.length)
  }, PLAY_INTERVAL)
}

/**
 * 进入/退出时间轴模式。
 *
 * 开关的核心不是"多一个图层"，而是"两种图不能同时显示"：
 * 静态图和动态图都是 0~1 的风险面，叠在一起是两张半透明图相加，
 * 颜色会比任何一天都红，看的人会以为 7 月 6 日比实际更危险。
 * 所以开时间轴就把静态图关掉（连勾选框一起改，避免界面自相矛盾）。
 */
function toggleTimeline() {
  if (!timeline.value) {
    console.warn('[时间轴] manifest 还没加载好，稍等一下或检查 /risk/risk_timeline.json')
    return
  }
  timelineOn.value = !timelineOn.value
  if (timelineOn.value) {
    // 先记下用户此刻的选择，再强制关掉静态贴图让它让位
    overlayBeforeTimeline = overlayOn.value
    overlayOn.value = false
    if (overlayLayer) overlayLayer.show = false
    legendFoot.value = TIMELINE_FOOT
    showFrame(0)                  // 总是从第一天开始，避免停在上一轮的帧号
  } else {
    stopPlay()
    // 拖动防抖的定时器也要清：退出时它还可能再补建一帧，
    // 那一帧建出来就是 show=false 的孤儿，白占显存
    if (slideTimer) { clearTimeout(slideTimer); slideTimer = null }
    for (const [, l] of frameCache) l.show = false
    frameLoading.value = false
    // 还回"进入时间轴之前"的状态，而不是写死 true：
    // 用户要是本来就不想看贴图（只想看底图），退出后不该被强行打开
    overlayOn.value = overlayBeforeTimeline
    if (overlayLayer) overlayLayer.show = overlayBeforeTimeline
    legendFoot.value = staticFoot // 原文还回去（加载失败时它就是空串，不会造出假数据）
  }
}

/**
 * 拖动滑块。
 *
 * 153 帧的滑块拖一次会连发几十个 input 事件，每个都建图层 = 几十次 11 MB
 * 贴图上传，主线程直接卡死。所以：帧号立刻改（日期和统计文字必须跟手），
 * 真正的建层往后推 70 ms，手停下来只执行最后一次。
 * 70 ms 是人感觉不出延迟、又足以把一整段拖动合并成一次的门槛。
 */
function onSlide(e) {
  if (playing.value) stopPlay()
  if (!timeline.value) return
  const n = timeline.value.frames.length
  frameIdx.value = Math.max(0, Math.min(n - 1, Number(e.target.value)))
  if (slideTimer) clearTimeout(slideTimer)
  slideTimer = setTimeout(() => {
    slideTimer = null
    showFrame(frameIdx.value)
  }, 70)
}

/** 当前帧对象。没有 manifest 时返回 null，模板里全部 v-if 兜住 */
const curFrame = computed(() => timeline.value?.frames?.[frameIdx.value] ?? null)

/** 面雨量：manifest 里是 5 个站各自的 mm，取平均当作"这一天的雨有多大" */
function rainOf(f) {
  const v = Object.values(f?.rain_mm ?? {})
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0
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

  // 调试用：把 viewer / Cesium 挂到 window 上，浏览器控制台里可以直接操作相机。
  // 比如 window.__viewer.camera.setView({
  //   destination: window.__Cesium.Cartesian3.fromDegrees(114.3, 30.6, 9000),
  // })
  // 截图脚本（pipeline/analysis 下的 *.cjs）也靠它精确取景，不依赖模拟鼠标滚轮。
  // Cesium 是 ESM import 进来的，不挂到 window 上，控制台里就没有这个全局变量。
  // 生产上留着无害——只是两个全局引用，不占内存也不影响性能。
  window.__viewer = viewer
  window.__Cesium = Cesium

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
  //      （原图 1656x1671 像素，约 90 m/像素）。要看清得切瓦片，那是后面的事。
  //      这个尺寸不是随手定的：它与点击查询用的 grid.bin 是同一个
  //      EPSG:4326 等经纬度网格，所以"看到的位置"和"点到的数值"严格对应。
  //      （早期版本贴图直接从 EPSG:4547 的矩形栅格降采样，那个投影矩形在
  //      经纬度下是个梯形，东北角会错 1.5 km —— 贴图与查询各说各话。）
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
    // 原文存一份：切到时间轴时这行会被换成提示语，退出时要还回来
    staticFoot = legendFoot.value

    console.log(`[贴图] ${meta.file} 已铺在 ${rectangle.west} ~ ${rectangle.east}`, meta.bbox_wgs84)
  } catch (err) {
    // 贴图失败不该让整页白屏：底图、区界、易涝区都还能用，只是少了指数
    console.error('[贴图] 加载失败，检查 frontend/public/overlay/ 下的 png 和 json', err)
  }

  // ---------------- 叠加注记层 ----------------
  // 只有影像没有地名，看不出这里是哪儿。再叠一层"影像注记"，
  // 顺序很重要：后 add 的盖在上面，所以注记要放在底图之后
  noteLayer = viewer.imageryLayers.addImageryProvider(tiandituLayer('cia', 'cia_w'))

  // ---------------- 加载动态风险时间轴（M6） ----------------
  // 数据来源是 risk_dynamic.py 的最后一步：它把 153 张 webp + manifest 复制到
  // frontend/public/risk/，所以这里的路径就是 /risk/xxx（public 是网站根目录）。
  //
  // 这里只做一件事：把 manifest 拿到手。**不建任何图层**——
  // 建层推迟到用户第一次点"时间轴"（见 showFrame），这样页面打开的第一个
  // 一秒钟不需要为时间轴付任何代价。
  //
  // 三层都要守住：
  //   1. 失败不能影响静态图：这段包在 try 里，manifest 挂了页面照样能用
  //   2. 不预建图层，避免 153 帧把显存吃穿
  //   3. 拿到 manifest 后仍要抬一次注记层，为后面动态建层定好基线
  try {
    const tlResp = await fetch('/risk/risk_timeline.json')
    if (!tlResp.ok) throw new Error(`HTTP ${tlResp.status}`)
    const tl = await tlResp.json()

    timeline.value = tl

    // 只预取第一帧。以前是 7 帧全预取（4.6 MB），现在 153 帧全预取是 36 MB，
    // 打开页面白等十几秒。第一帧够让"点开时间轴"的第一眼不是空白。
    const first = new Image()
    first.src = `/risk/${tl.frames[0].img}`

    // 注记顶回最上面。这一步不能省：imageryLayers 是"后 add 的压在上面"，
    // 后面每建一帧动态图层都会压上来，所以 ensureLayer 里也会再抬一次。
    if (noteLayer) viewer.imageryLayers.raiseToTop(noteLayer)
    console.log(`[时间轴] 已载入 ${tl.frames.length} 帧：${tl.frames[0].date} ~ ${tl.frames.at(-1).date}（按需加载）`)
  } catch (err) {
    // 时间轴加载失败只是个功能缺失，不该让整页白屏或抛错中断后面的初始化
    console.warn('[时间轴] 加载失败，时间轴按钮将不可用', err)
  }

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
  // 40 个点，来源是武汉市水务局公布的历史易涝点清单（M5-3 入库）。
  // 每个点的几何是「以坐标为中心、半径 100 米的圆」——官方只公布点位，
  // 不给实际积水范围，所以用统一的缓冲圈表示"这一带"，
  // 不假装知道它到底漫到哪条街（编一个多边形面积经不起追问）。
  // 半径 100 米用 geography 算，保证在任意纬度都是真的 100 米。
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

    // ---- 每个点再加一个"针尖" ----
    // 为什么：100 米半径的圆，在 12 km 高空只有约 11 个像素，
    // 40 个点散在卫星图里，和天地图自带的红色路牌（高速编号）混在一起，
    // 第一眼根本分不出哪些是我们的数据。
    //
    // 关键在于 point 的 pixelSize 单位是**屏幕像素**，不随缩放变化——
    // 这是 Cesium 里唯一天然"恒定屏幕尺寸"的图元。
    // 多边形表达"这一带有多大"，针尖保证"任何缩放级别都看得见"，两者分工。
    //
    // 位置取环上 33 个顶点的平均值。我们的环是圆规画的（ST_Buffer 生成），
    // 顶点均值就是圆心，不需要再从后端多传一个 lon/lat 字段——
    // 数据只存一份，就不会出现"点搬家了针尖没搬"的对不齐。
    const hierarchy = entity.polygon?.hierarchy?.getValue()
    const ring = hierarchy?.positions
    if (ring && ring.length) {
      let sx = 0, sy = 0, sz = 0
      for (const p of ring) { sx += p.x; sy += p.y; sz += p.z }
      entity.position = new Cesium.Cartesian3(sx / ring.length, sy / ring.length, sz / ring.length)
      entity.point = {
        pixelSize: 7,
        color,
        outlineColor: Cesium.Color.WHITE, // 白边保证压在任何底图上都看得清
        outlineWidth: 1.5,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      }
    }
  }

  // ---------------- 点击查询 ----------------
  // 整条链路：屏幕像素 → 经纬度 → 后端 → 栅格里的一个格子 → 卡片
  //
  // 关键在第一步的换算，Cesium 分了两个动作：
  //   pickEllipsoid     从相机穿过这个像素发一条射线，求它与地球椭球面的交点
  //   Cartographic.fromCartesian  把三维坐标拆成（经度, 纬度, 高度），弧度制
  //
  // 注意 pickEllipsoid 求的是"椭球面"，不是"真实地面"。
  // 我们没开地形，两者是一回事；将来开了地形，这里要换成 globe.pick。
  clickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas)

  /**
   * 查询一个经纬度并弹卡片。
   * 抽成独立函数有两个好处：
   *   1. 点击事件只负责"屏幕 → 经纬度"，查询逻辑可以单独测
   *   2. 将来做"搜索框定位""链接分享某个点"时，直接调这个函数，不用伪造鼠标事件
   */
  async function queryAt(lon, lat, screenX, screenY, cartesian) {
    // 先落一个标记：卡片会挡住点击处，标记不会，让人知道点的是哪
    if (pickEntity) viewer.entities.remove(pickEntity)
    pickEntity = viewer.entities.add({
      position: cartesian,
      point: {
        pixelSize: 7,
        color: Cesium.Color.WHITE,
        outlineColor: Cesium.Color.fromCssColorString('#f85149'),
        outlineWidth: 2,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        // 让标记永远画在最前面，不会被贴图或多边形挡住
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    })

    // 卡片位置：鼠标右下方 16 像素；贴近屏幕边缘时往回缩
    const canvas = viewer.scene.canvas
    const CARD_W = 210         // 略大于卡片实际宽度，留点余量
    const CARD_H = 190
    const x = Math.max(Math.min(screenX + 16, canvas.clientWidth - CARD_W), 8)
    const y = Math.max(Math.min(screenY + 16, canvas.clientHeight - CARD_H), 8)

    try {
      // 走 vite 代理到 8080；经纬度保留 6 位小数，约 0.1 m 精度，足够
      const resp = await fetch(
        `/api/flood-index?lon=${lon.toFixed(6)}&lat=${lat.toFixed(6)}`)
      const data = await resp.json()
      pick.value = { ...data, lon, lat, x, y }
    } catch (e) {
      pick.value = {
        status: 'error',
        message: `查询失败：${e.message}。检查后端是否在跑（8080）。`,
        lon, lat, x, y,
      }
    }
  }

  clickHandler.setInputAction((movement) => {
    // 框选模式下左键是"画框"，不该弹单点卡片；
    // 拖完手松开时 Cesium 还会补一个 LEFT_CLICK，用时间戳挡掉
    if (selectMode.value || performance.now() < suppressClickUntil) {
      return
    }
    const cartesian = viewer.camera.pickEllipsoid(
      movement.position, viewer.scene.globe.ellipsoid)
    if (!cartesian) {
      pick.value = null       // 点到太空里了，不显示卡片
      return
    }
    const carto = Cesium.Cartographic.fromCartesian(cartesian)
    queryAt(
      Cesium.Math.toDegrees(carto.longitude),
      Cesium.Math.toDegrees(carto.latitude),
      movement.position.x,
      movement.position.y,
      cartesian,
    )
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK)

  // ---------------- 框选统计 ----------------
  // 三个事件拼成一个手势：
  //   LEFT_DOWN  记起点，顺手把相机控制关掉（否则拖框会变成转地球）
  //   MOUSE_MOVE 实时更新矩形，让人看见自己在框哪儿
  //   LEFT_UP    算出范围、请求后端、退出框选模式
  //
  // 为什么不用"Cesium 自带的矩形绘制工具"：那套是给编辑器用的，
  // 要引入额外的 widget 样式。十几行事件就够，还能看清每一步。

  /** 屏幕像素 → 经纬度。点不到地球（点到太空）时返回 null */
  function screenToLonLat(position) {
    const cartesian = viewer.camera.pickEllipsoid(
      position, viewer.scene.globe.ellipsoid)
    if (!cartesian) {
      return null
    }
    const carto = Cesium.Cartographic.fromCartesian(cartesian)
    return {
      lon: Cesium.Math.toDegrees(carto.longitude),
      lat: Cesium.Math.toDegrees(carto.latitude),
    }
  }

  /** 画（或重画）那个黄色选框。每次移动都重建实体是因为 Rectangle 不能原地改坐标 */
  function drawRect(w, s, e, n) {
    if (rectEntity) {
      viewer.entities.remove(rectEntity)
    }
    const color = Cesium.Color.fromCssColorString('#ffd33d')
    rectEntity = viewer.entities.add({
      rectangle: {
        coordinates: Cesium.Rectangle.fromDegrees(w, s, e, n),
        material: color.withAlpha(0.25),
        outline: true,
        outlineColor: color,
        outlineWidth: 2,
      },
    })
  }

  /** 请求后端统计。参数用 6 位小数，约 0.1 m，和单点查询保持一致 */
  async function loadStats(w, s, e, n) {
    stats.value = { status: 'loading' }
    const t0 = performance.now()
    try {
      const q = `west=${w.toFixed(6)}&south=${s.toFixed(6)}`
        + `&east=${e.toFixed(6)}&north=${n.toFixed(6)}`
      const resp = await fetch(`/api/flood-index/stats?${q}`)
      const data = await resp.json()
      stats.value = { ...data, ms: Math.round(performance.now() - t0) }
    } catch (err) {
      stats.value = {
        status: 'error',
        message: `统计失败：${err.message}。检查后端是否在跑（8080）。`,
      }
    }
  }

  clickHandler.setInputAction((movement) => {
    if (!selectMode.value) {
      return
    }
    const p = screenToLonLat(movement.position)
    if (!p) {
      return
    }
    dragStart = { lon: p.lon, lat: p.lat }
    // 关键：拖框期间把相机锁住，松手时一定要还回来（见 LEFT_UP）
    viewer.scene.screenSpaceCameraController.enableInputs = false
    // 上一轮的卡片和框先清掉，不然拖的时候新旧两个框叠在一起
    stats.value = null
    clearRect()
  }, Cesium.ScreenSpaceEventType.LEFT_DOWN)

  clickHandler.setInputAction((movement) => {
    if (!dragStart) {
      return
    }
    const p = screenToLonLat(movement.position)
    if (!p) {
      return
    }
    // 起点可能比终点更东/更北，四个值都要 min/max 一次，
    // 直接拿 start/end 当 west/north 会画出"负宽度"的框，Cesium 不报错但什么都不显示
    drawRect(
      Math.min(dragStart.lon, p.lon),
      Math.min(dragStart.lat, p.lat),
      Math.max(dragStart.lon, p.lon),
      Math.max(dragStart.lat, p.lat),
    )
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE)

  clickHandler.setInputAction((movement) => {
    if (!dragStart) {
      return
    }
    const start = dragStart
    dragStart = null
    viewer.scene.screenSpaceCameraController.enableInputs = true
    suppressClickUntil = performance.now() + 300

    const p = screenToLonLat(movement.position)
    if (!p) {
      return
    }
    const w = Math.min(start.lon, p.lon)
    const e = Math.max(start.lon, p.lon)
    const s = Math.min(start.lat, p.lat)
    const n = Math.max(start.lat, p.lat)

    // 0.002° 在武汉约 200 m。太小的框多半是"手滑点了一下"，
    // 统计出来只有几格，还会让人以为程序算错了
    if (e - w < 0.002 || n - s < 0.002) {
      stats.value = { status: 'error', message: '框太小了，至少拖出 200 米见方' }
      return
    }

    drawRect(w, s, e, n)
    selectMode.value = false
    loadStats(w, s, e, n)
  }, Cesium.ScreenSpaceEventType.LEFT_UP)

  // Esc 退出框选：拖到一半反悔时用
  window.addEventListener('keydown', onKeyDown)
})

// 组件销毁时一定要 destroy，否则 GPU 显存不释放，热更新几次浏览器就崩了。
// 这是 Cesium + Vue 最容易踩的坑。
onBeforeUnmount(() => {
  // 播放定时器不是 Cesium 对象，viewer.destroy() 管不到它。
  // 不清的话，热更新后旧组件的定时器还在跑，而它引用的图层已经销毁——
  // 控制台会刷 "layer is destroyed"，并且越积越多，最后卡死。
  stopPlay()
  // 事件句柄不释放，热更新几次会攒下一堆监听器：点一下弹好几张卡片
  if (clickHandler && !clickHandler.isDestroyed()) {
    clickHandler.destroy()
    clickHandler = null
  }
  window.removeEventListener('keydown', onKeyDown)
  // 拖框时把相机锁住了，组件销毁前必须还回来，
  // 否则下一个页面的地图会完全不能拖动（看起来像"卡死"）
  if (viewer && !viewer.isDestroyed()) {
    viewer.scene.screenSpaceCameraController.enableInputs = true
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
    <!-- 工具栏：目前只有一个按钮，将来"刷新图层""导出"都往这里加 -->
    <div class="tools">
      <button :class="['tool-btn', { active: timelineOn }]" @click="toggleTimeline">
        {{ timelineOn ? '退出时间轴' : '时间轴' }}
      </button>
      <button :class="['tool-btn', { active: selectMode }]" @click="toggleSelect">
        {{ selectMode ? '退出框选' : '框选统计' }}
      </button>
    </div>

    <!--
      时间轴面板（M6）：只有开着时间轴并且 manifest 加载成功才出现。
      用 v-if="timelineOn && timeline" 而不是 v-show：没数据时不该露出一个空壳子，
      那会让人以为"功能坏了"，而实际只是 /risk/risk_timeline.json 还没生成。
    -->
    <aside v-if="timelineOn && timeline" class="timeline">
      <div class="tl-head">
        <button class="tl-play" :title="playing ? '暂停' : '播放'" @click="togglePlay">
          {{ playing ? '⏸' : '▶' }}
        </button>
        <b class="tl-date">{{ curFrame ? curFrame.date : '—' }}</b>
        <span class="tl-pos">
          第 {{ frameIdx + 1 }} / {{ timeline.frames.length }} 帧<template v-if="frameLoading"> · 载入中…</template>
        </span>
      </div>

      <!-- 用 @input 不用 @change：拖动过程中图就要跟着变 -->
      <input
        class="tl-range"
        type="range"
        min="0"
        :max="timeline.frames.length - 1"
        step="1"
        :value="frameIdx"
        @input="onSlide"
      />

      <p v-if="curFrame" class="tl-foot">
        面雨量 {{ rainOf(curFrame).toFixed(1) }} mm ·
        API {{ curFrame.api_mean.toFixed(0) }} mm ·
        系数 {{ curFrame.factor_min.toFixed(2) }}~{{ curFrame.factor_max.toFixed(2) }} ·
        极高 {{ curFrame.area_extreme_km2.toLocaleString() }} km²
      </p>
    </aside>

    <p v-if="selectMode" class="hint">
      按住左键拖出一个矩形，统计框内的风险构成（Esc 退出）
    </p>

    <!-- 框选统计结果 -->
    <aside v-if="stats" class="stats">
      <button class="pick-close" title="关闭" @click="stats = null">×</button>
      <div class="stats-head">框选统计</div>

      <p v-if="stats.status === 'loading'" class="pick-msg">统计中…</p>
      <p v-else-if="stats.status !== 'ok'" class="pick-msg">{{ stats.message }}</p>

      <template v-else>
        <div class="stats-num">
          <div><b>{{ num(stats.mean, 3) }}</b><span>平均</span></div>
          <div><b>{{ num(stats.median, 3) }}</b><span>中位</span></div>
          <div><b>{{ num(stats.max, 3) }}</b><span>最高</span></div>
        </div>

        <!-- 堆叠条：五档占比拼成一条，比五个数字更快看出结构 -->
        <div class="bar">
          <i v-for="lv in stats.levels" :key="lv.name"
             :style="{ width: lv.pct + '%', background: lv.color }"></i>
        </div>

        <ul class="stats-rows">
          <li v-for="lv in stats.levels" :key="lv.name">
            <i :style="{ background: lv.color }"></i>
            <span>{{ lv.name }}</span>
            <b>{{ lv.km2.toFixed(1) }} km² · {{ lv.pct.toFixed(1) }}%</b>
          </li>
        </ul>

        <p class="legend-foot">
          框内 {{ stats.bbox_km2.toFixed(1) }} km²，其中参与评价
          {{ stats.area_km2.toFixed(1) }} km²；水体与市域外
          {{ stats.no_data.toLocaleString() }} 格不计 · {{ stats.ms }} ms
        </p>
      </template>
    </aside>

    <!--
      图例浮层：和 App.vue 里那块状态面板同一套配色变量（--panel 等）。
      为什么要放在这个组件里而不是 App.vue？
      因为它只服务于"贴图"这一件事，跟着组件走，删掉组件图例自然消失，不会留孤儿。
    -->
    <aside v-if="legend.length" class="legend">
      <div class="legend-head">
        <span>易涝风险指数</span>
        <!-- 时间轴模式下这个勾选框没有意义（静态贴图已被强制关掉，改它只会
             把静态图垫到动态帧下面，污染当天颜色），所以禁用 + 说清原因。
             只说"禁用"不说"为什么"，用户会以为界面坏了。 -->
        <label
          :class="['switch', { disabled: timelineOn }]"
          :title="timelineOn
            ? '时间轴模式下由滑块控制图层，退出后可切换'
            : '显示 / 隐藏静态指数贴图'"
        >
          <input v-model="overlayOn" type="checkbox" :disabled="timelineOn" @change="toggleOverlay" />
          <span>贴图</span>
        </label>
      </div>

      <ul>
        <li v-for="lv in legend" :key="lv.name">
          <i :style="{ background: lv.color }"></i>
          <span>{{ lv.name }}</span>
          <!-- 三位小数：分级阈值是 p35/p65/p88/p97 算出来的 0.2481/0.3761/0.5593/0.6849，
               只留两位会造出 0.0007 宽的"缝"——落在 0.5593~0.56 的点卡片说"高"、
               图例却说"中"。宁可难看一点，不要自相矛盾。上限 1.001 夹到 1.000 显示。 -->
          <b>{{ lv.lo.toFixed(3) }}~{{ Math.min(lv.hi, 1).toFixed(3) }}</b>
        </li>
      </ul>

      <p class="legend-foot">{{ legendFoot }}</p>
    </aside>

    <!--
      点击查询卡片：位置由 JS 按点击点算出（left/top 用内联 style）。
      用 v-if 而不是 v-show：不显示的时候连 DOM 都不要，省得它挡住鼠标事件。
    -->
    <div v-if="pick" class="pick" :style="{ left: pick.x + 'px', top: pick.y + 'px' }">
      <button class="pick-close" title="关闭" @click="pick = null">×</button>

      <template v-if="pick.status === 'ok'">
        <div class="pick-head">
          <i :style="{ background: pick.color }"></i>
          <b>{{ pick.level }}</b>
          <span class="pick-val">{{ pick.index.toFixed(3) }}</span>
        </div>
        <ul>
          <li><span>高程</span><b>{{ pick.elev.toFixed(1) }} m</b></li>
          <li><span>坡度</span><b>{{ pick.slope.toFixed(2) }}°</b></li>
          <li><span>汇流</span><b>{{ pick.acc.toLocaleString() }}</b></li>
          <li><span>经纬度</span><b>{{ pick.lon.toFixed(4) }}, {{ pick.lat.toFixed(4) }}</b></li>
        </ul>
        <p class="pick-note">指数 = 高程/坡度/填洼/汇流的加权和，0~1，越大越易涝</p>
        <!-- 时间轴模式下必须说清楚：这里查的是"地形"，不随日期变 -->
        <p v-if="timelineOn" class="pick-note">
          地形指数不随降雨变化；当前帧的动态风险见下方时间轴
        </p>
      </template>

      <p v-else class="pick-msg">{{ pick.message || '没有取到数据' }}</p>
    </div>
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

/* ---------- 框选工具 ---------- */
.tools {
  position: absolute;
  top: 14px;
  right: 16px;
  z-index: 22;
  /* 两个按钮并排：时间轴 + 框选统计 */
  display: flex;
  gap: 8px;
}

.tool-btn {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  backdrop-filter: blur(10px);
  cursor: pointer;
}

.tool-btn:hover {
  border-color: var(--accent);
}

/* 激活时整颗按钮变黄：让人一眼知道"现在拖鼠标是在画框，不是转地球" */
.tool-btn.active {
  font-weight: 600;
  color: #1a1a1a;
  background: #ffd33d;
  border-color: #ffd33d;
}

.hint {
  position: absolute;
  top: 16px;
  left: 50%;
  z-index: 22;
  padding: 6px 14px;
  font-size: 12px;
  color: #ffd33d;
  white-space: nowrap;
  background: rgba(20, 24, 30, 0.82);
  border: 1px solid rgba(255, 211, 61, 0.35);
  border-radius: 999px;
  transform: translateX(-50%);
  pointer-events: none;    /* 别挡住下面的画布 */
}

/* ---------- 时间轴（M6） ---------- */
.timeline {
  position: absolute;
  left: 50%;
  bottom: 24px;
  z-index: 23;                      /* 高于图例(20)/统计卡片(21)/工具栏(22)，别被压住 */
  /*
   * 宽度要能收，不能写死 660px：
   *   底栏居中，右下角还坐着 208px 宽的图例。视口一旦窄到 1104px 以下，
   *   两者就会叠上——而底栏 z-index 更高，被切掉的是图例右列那串阈值数字
   *   （0.685~1.000），恰好是最需要看的一档。
   *
   *   calc(100% - 480px) 里的 480 是"必须让出去的横向空间"，两半对称算：
   *     右侧：图例 208 + 右边距 16 = 224
   *     左侧：居中底栏的另一半也要留同样 224
   *     再加两条 16 的间隙   → 224×2 + 16×2 = 480
   *   于是任意视口宽度下两者都至少隔 16px，不需要媒体查询、也不会出现
   *   "断点差 1px 就重叠"的老问题。660 只是舒适上限。
   *   1024 视口时收成 544px，滑块照样能拖（可拖长度约 500px）。
   */
  width: min(660px, calc(100% - 480px));
  padding: 10px 16px 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  backdrop-filter: blur(10px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
  color: var(--text);
  transform: translateX(-50%);
  user-select: none;
}

.tl-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.tl-play {
  flex: none;
  width: 26px;
  height: 26px;
  padding: 0;
  font-size: 12px;
  line-height: 1;
  color: #1a1a1a;
  background: #ffd33d;
  border: none;
  border-radius: 50%;
  cursor: pointer;
}

.tl-play:hover {
  filter: brightness(1.08);
}

.tl-date {
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}

.tl-pos {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

/* 滑块不重写 thumb：accent-color 一行就能把浏览器默认的蓝色换成主题黄，
   跨 Chrome/Edge/Firefox 都对，省掉一堆 ::-webkit-slider-thumb 前缀 */
.tl-range {
  width: 100%;
  margin: 8px 0 6px;
  accent-color: #ffd33d;
  cursor: pointer;
}

.tl-foot {
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

/* ---------- 框选统计卡片 ---------- */
.stats {
  position: absolute;
  top: 56px;
  right: 16px;
  z-index: 21;
  width: 236px;
  padding: 12px 14px;
  font-size: 12px;
  color: var(--text);
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  backdrop-filter: blur(10px);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
}

.stats-head {
  margin-bottom: 8px;
  font-weight: 600;
}

.stats-num {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}

.stats-num div {
  flex: 1;
  padding: 6px 0;
  text-align: center;
  background: rgba(255, 255, 255, 0.04);
  border-radius: 6px;
}

.stats-num b {
  display: block;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}

.stats-num span {
  font-size: 10px;
  color: var(--text-dim);
}

.bar {
  display: flex;
  height: 8px;
  margin-bottom: 8px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 4px;
}

.bar i {
  display: block;
  height: 100%;
}

.stats-rows {
  list-style: none;
}

.stats-rows li {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 0;
}

.stats-rows i {
  flex: none;
  width: 9px;
  height: 9px;
  border-radius: 2px;
}

.stats-rows span {
  flex: 1;
}

.stats-rows b {
  font-weight: 400;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
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

/* 禁用态必须有视觉区分：只把 input 设成 disabled，外观几乎没变化，
   用户会连点几下然后来问"贴图开关坏了"。变灰 + 禁止手势是最省事的提示。 */
.switch.disabled,
.switch.disabled input {
  cursor: not-allowed;
}

.switch.disabled {
  opacity: 0.45;
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

/* ---------- 点击查询卡片 ---------- */
.pick {
  position: absolute;
  z-index: 21;              /* 比图例再高一层 */
  width: 198px;             /* 和 JS 里的 CARD_W 对得上（含 padding） */
  padding: 10px 12px 8px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 9px;
  backdrop-filter: blur(10px);
  font-size: 12px;
  color: var(--text);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
}

.pick-close {
  position: absolute;
  top: 4px;
  right: 6px;
  width: 16px;
  height: 16px;
  line-height: 14px;
  font-size: 14px;
  color: var(--text-dim);
  background: transparent;
  border: none;
  cursor: pointer;
}

.pick-close:hover {
  color: var(--text);
}

.pick-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.pick-head i {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  flex: none;
}

.pick-head b {
  font-size: 13px;
}

.pick-val {
  margin-left: auto;
  margin-right: 14px;       /* 让开关闭按钮 */
  font-variant-numeric: tabular-nums;
  color: var(--text-dim);
}

.pick ul {
  list-style: none;
}

.pick li {
  display: flex;
  justify-content: space-between;
  padding: 2px 0;
}

.pick li span {
  color: var(--text-dim);
}

.pick li b {
  font-weight: 400;
  font-variant-numeric: tabular-nums;
}

.pick-note {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed rgba(255, 255, 255, 0.08);
  font-size: 10px;
  line-height: 1.5;
  color: var(--text-dim);
}

.pick-msg {
  padding-right: 12px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-dim);
}

/* Cesium 自带的版权信息默认在左下角，字号偏小，这里统一调一下 */
:deep(.cesium-viewer-bottom) {
  left: 12px;
  bottom: 8px;
}
</style>
