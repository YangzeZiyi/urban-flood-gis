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

onMounted(async () => {
  // ---------------- 构造 Viewer ----------------
  viewer = new Cesium.Viewer(container.value, {
    // 底图。这里用 Cesium 自带的离线底图 NaturalEarthII：
    // 不需要 Ion token，不需要联网，装完就能看到球。缺点是分辨率低。
    // 想换成在线高清底图，见文件末尾的注释。
        baseLayer: new Cesium.ImageryLayer(
      new Cesium.UrlTemplateImageryProvider({
        url: 'https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
        subdomains: ['1', '2', '3', '4'],
        maximumLevel: 18,
      }),
    ),

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
    // 从后端拿易涝区数据，加载到地球上
  const zones = await Cesium.GeoJsonDataSource.load('/api/zones', { clampToGround: true })
  viewer.dataSources.add(zones)
  await viewer.flyTo(zones, { duration: 1.5 })

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
 * 想换成在线高清底图？
 * ---------------------------------------------------------------------------
 * 方案 A：Cesium Ion（官方，需要注册免费账号拿 token）
 *   Cesium.Ion.defaultAccessToken = '你的token'
 *   然后把 baseLayer 改成 Cesium.ImageryLayer.fromProviderAsync(
 *     Cesium.IonImageryProvider.fromAssetId(2))   // 2 = Bing 影像
 *
 * 方案 B：天地图（国内最常用，需要申请 key，坐标是 CGCS2000，和我们的数据一致）
 *   new Cesium.WebMapTileServiceImageryProvider({
 *     url: 'http://t{s}.tianditu.gov.cn/img_w/wmts?tk=你的key',
 *     layer: 'img', style: 'default', tileMatrixSetID: 'w',
 *     subdomains: ['0','1','2','3','4','5','6','7'], maximumLevel: 18,
 *   })
 *
 * 方案 C：高德卫星图（不用 key，但它是 GCJ-02 火星坐标，
 *   和我们 WGS84 的易涝区数据会有几百米偏移，正式成果不要用）
 */
</script>

<template>
  <!-- ref="container" 把这个 div 的 DOM 交给上面的 container 变量 -->
  <div ref="container" class="cesium-container"></div>
</template>

<style scoped>
.cesium-container {
  width: 100%;
  height: 100%;
  background: #000;
}

/* Cesium 自带的版权信息默认在左下角，字号偏小，这里统一调一下 */
:deep(.cesium-viewer-bottom) {
  left: 12px;
  bottom: 8px;
}
</style>
