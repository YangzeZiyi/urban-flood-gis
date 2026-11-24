/**
 * api.js —— 数据来源的唯一入口
 * ===========================================================================
 * 这个文件存在的理由：同一套界面要能跑在两种后端上。
 *
 *   ① 本机/服务器模式（默认）：数据来自 Spring Boot 的 5 个接口，
 *      查询走数据库和 .bin 的随机读，统计在 Java 里算。
 *   ② 静态模式（`npm run build:static`）：没有进程、没有数据库，
 *      数据是导出好的文件——易涝点和边界是 GeoJSON，点查按 0.1° 分块，
 *      框选用的是一整幅 uint8 索引网格，统计在浏览器里算。
 *
 * 为什么不在组件里判断：判断散在各处，迟早会漏一处。
 * 这里把「拿数据」收成一个函数集，组件只管调，不关心数据从哪来。
 *
 * 静态版的数据由 pipeline/publish/export_static.py 生成，
 * 目录 frontend/public/static-data/，格式说明见同目录的 grid-meta.json。
 */

/** 构建期开关：由 `vite build --mode static` 注入（见 .env.static） */
export const isStatic = import.meta.env.VITE_STATIC_DATA === '1'

const BASE = '/static-data'
const NO_DATA = 0xffff

// ---------------------------------------------------------------------------
// 静态模式下的资源加载（带缓存：同一个块/同一张表不重复下载）
// ---------------------------------------------------------------------------

let metaPromise = null
/** 网格元数据：bbox、分辨率、分块大小、量化区间、分级表 */
function meta() {
  if (!metaPromise) {
    metaPromise = fetch(`${BASE}/grid-meta.json`).then((r) => {
      if (!r.ok) throw new Error(`grid-meta.json HTTP ${r.status}`)
      return r.json()
    })
  }
  return metaPromise
}

const tileCache = new Map()
/**
 * 取一块点查网格。key 是 "tx_ty"，缓存在内存里——
 * 同一片区域反复点，只有第一次会发请求。
 */
async function tile(m, tx, ty) {
  const key = `${tx}_${ty}`
  if (tileCache.has(key)) return tileCache.get(key)
  const p = fetch(`${BASE}/tiles/${key}.bin`).then((r) => {
    if (!r.ok) throw new Error(`tiles/${key}.bin HTTP ${r.status}`)
    return r.arrayBuffer()
  })
  tileCache.set(key, p)
  return p
}

let statsPromise = null
/** 框选统计用的降采样网格，只下一次 */
function statsGrid(m) {
  if (!statsPromise) {
    statsPromise = fetch(`${BASE}/${m.stats.file}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${m.stats.file} HTTP ${r.status}`)
        return r.arrayBuffer()
      })
      .then((buf) => new DataView(buf))
  }
  return statsPromise
}

/** uint16 反量化。log1p 通道（汇流）要还原成"格数" */
function decode(ch, raw) {
  if (raw === NO_DATA) return null
  const x = raw / (NO_DATA - 1)
  if (ch.type === 'log1p') {
    return Math.round(10 ** (ch.min + x * (ch.max - ch.min)) - 1)
  }
  return ch.min + x * (ch.max - ch.min)
}

/** 按分级表取等级（lo ≤ v < hi，最后一档闭合到 1） */
function levelOf(m, v) {
  for (const lv of m.levels) {
    if (v >= lv.lo && (v < lv.hi || lv === m.levels[m.levels.length - 1])) return lv
  }
  return m.levels[m.levels.length - 1]
}

// ---------------------------------------------------------------------------
// 对外接口
// ---------------------------------------------------------------------------

/** 易涝点 GeoJSON 的地址（Cesium 的 GeoJsonDataSource 只要地址） */
export function zonesUrl() {
  return isStatic ? `${BASE}/zones.geojson` : '/api/zones?minLevel=3'
}

/** 区县边界的地址 */
export function adminUrl() {
  return isStatic ? `${BASE}/admin.geojson` : '/api/admin?level=district'
}

/** 状态栏信息 */
export async function loadHealth() {
  if (!isStatic) {
    const res = await fetch('/api/health')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.json()
  }
  const res = await fetch(`${BASE}/health.json`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

/**
 * 单点查询。返回结构与后端 `/api/flood-index` 一致：
 * { status:'ok', index, level, color, elev, slope, acc, lon, lat, row, col }
 * 或 { status:'outside', message, ... }
 */
export async function queryFloodIndex(lon, lat) {
  const l6 = Number(lon.toFixed(6))
  const a6 = Number(lat.toFixed(6))

  if (!isStatic) {
    const resp = await fetch(`/api/flood-index?lon=${l6}&lat=${a6}`)
    return resp.json()
  }

  const m = await meta()
  const [w, , , n] = m.bbox
  const col = Math.floor((l6 - w) / m.dx)
  const row = Math.floor((n - a6) / m.dy)

  if (col < 0 || col >= m.width || row < 0 || row >= m.height) {
    // 和后端一致：越界时不给行列号
    return { status: 'outside', lon: l6, lat: a6, message: '这一点不在武汉市域内，或落在水体上，不参与易涝评价' }
  }

  const T = m.tile_cells
  const tx = Math.floor(col / T)
  const ty = Math.floor(row / T)
  const buf = await tile(m, tx, ty)
  const dv = new DataView(buf)

  // 块内布局：行优先、4 通道交错、每通道 uint16（小端），共 8 字节/格
  const off = ((row % T) * T + (col % T)) * 8
  const ch = {}
  for (let i = 0; i < m.channels.length; i++) {
    ch[m.channels[i].name] = decode(m.channels[i], dv.getUint16(off + i * 2, true))
  }

  if (ch.index === null) {
    return {
      status: 'outside',
      lon: l6,
      lat: a6,
      row,
      col,
      message: '这一点不在武汉市域内，或落在水体上，不参与易涝评价',
    }
  }

  const lv = levelOf(m, ch.index)
  return {
    status: 'ok',
    lon: l6,
    lat: a6,
    row,
    col,
    index: Number(ch.index.toFixed(3)),
    level: lv.name,
    color: lv.color,
    elev: Number(ch.elev.toFixed(1)),
    slope: Number(ch.slope.toFixed(2)),
    acc: ch.acc,
  }
}

// ---------------------------------------------------------------------------
// 面积换算
// ---------------------------------------------------------------------------
// 网格是按「经纬度等分」的，不是按米：1 度经度在赤道 111.3 km，在 30°N 只有
// 96.4 km。所以每一行都要按自己那行的纬度单独算格面积，用一个常数凑出来的
// 全市面积能差几十平方公里。两个常数和后端 FloodGrid 里那几个保持一致，
// 不然前后端算出的面积会各偏一点，对比时说不清是谁的问题。
const KM_PER_DEG_LAT = 110.574
const KM_PER_DEG_LON_EQUATOR = 111.320

/** 一个经纬度格子的面积（km²），dx/dy 是这一格在经/纬方向上的跨度（度） */
function cellKm2(lat, dx, dy) {
  return (dy * KM_PER_DEG_LAT) * (dx * KM_PER_DEG_LON_EQUATOR * Math.cos((lat * Math.PI) / 180))
}

/** 矩形本身的球面近似面积，和后端 bboxAreaKm2() 同式 */
function boxKm2(w, s, e, n) {
  const midLat = ((s + n) / 2) * (Math.PI / 180)
  return ((e - w) * KM_PER_DEG_LON_EQUATOR * Math.cos(midLat)) * ((n - s) * KM_PER_DEG_LAT)
}

/** 已排序数组的分位数。空数组返回 NaN。取整方式和后端 percentile() 一致 */
function percentile(sorted, p) {
  if (!sorted.length) return NaN
  const i = Math.round(p * (sorted.length - 1))
  return sorted[Math.max(0, Math.min(sorted.length - 1, i))]
}

function round3(v) {
  return Number.isNaN(v) ? v : Math.round(v * 1000) / 1000
}

/**
 * 框选统计。返回结构与后端 `/api/flood-index/stats` 字段一一对应。
 *
 * 静态版读的是整幅 uint8 索引网格：分辨率 1/254 = 0.004，
 * 面积按每行各自的纬度算（和后端同一套常数），所以面积、分档占比、
 * 均值中位数在界面显示的小数位上和后端一致。唯一的差别是索引被量化过，
 * 落在分级阈值正上方 0.004 以内的格子可能被分到相邻一档。
 */
export async function queryFloodStats(west, south, east, north) {
  if (!isStatic) {
    const q = `west=${west.toFixed(6)}&south=${south.toFixed(6)}`
      + `&east=${east.toFixed(6)}&north=${north.toFixed(6)}`
    const resp = await fetch(`/api/flood-index/stats?${q}`)
    return resp.json()
  }

  if (!(west < east) || !(south < north)) {
    return { status: 'error', message: '经纬度顺序不对：需要 west < east 且 south < north' }
  }

  const m = await meta()
  const S = m.stats
  const dv = await statsGrid(m)
  const [W, S0, E, N] = m.bbox

  // 先把框裁到网格范围内——用户可以把矩形拖到省外、拖到海里
  const ww = Math.max(west, W)
  const ee = Math.min(east, E)
  const ss = Math.max(south, S0)
  const nn = Math.min(north, N)
  if (ww >= ee || ss >= nn) {
    return { status: 'empty', message: '框选范围与网格没有交集（拖到武汉市域外了）' }
  }

  // 经纬度 → 行列号，和后端 FloodGrid.colOf()/rowOf() 同一套公式（含两端、夹在范围内）。
  // 这里必须和点查用同一套取整，否则「点一下」和「框住它」会给出不同的格子。
  const colOf = (lon) =>
    Math.max(0, Math.min(S.width - 1, Math.floor((lon - W) / S.dx)))
  const rowOf = (lat) =>
    Math.max(0, Math.min(S.height - 1, Math.floor((N - lat) / S.dy)))

  const c0 = colOf(ww)
  const c1 = colOf(ee)
  const r0 = rowOf(nn) // 北边界 → 小行号
  const r1 = rowOf(ss) // 南边界 → 大行号

  const cells = (c1 - c0 + 1) * (r1 - r0 + 1)
  const counts = m.levels.map(() => 0)
  const areaByLevel = m.levels.map(() => 0)
  const values = []
  let evaluated = 0
  let areaKm2 = 0
  let sum = 0
  let max = -Infinity
  let maxAt = null

  for (let r = r0; r <= r1; r++) {
    // 这一行格子的纬度（行中心）决定东西向宽度
    const lat = N - (r + 0.5) * S.dy
    const a = cellKm2(lat, S.dx, S.dy)
    const rowBase = r * S.width
    for (let c = c0; c <= c1; c++) {
      const raw = dv.getUint8(rowBase + c)
      if (raw === S.no_data) continue
      const v = raw / S.scale
      const li = m.levels.indexOf(levelOf(m, v))
      evaluated += 1
      sum += v
      areaKm2 += a
      counts[li] += 1
      areaByLevel[li] += a
      values.push(v)
      if (v > max) {
        max = v
        maxAt = { lon: W + (c + 0.5) * S.dx, lat }
      }
    }
  }

  if (!evaluated) {
    return { status: 'empty', message: '框选范围与网格没有交集（拖到武汉市域外了）' }
  }

  values.sort((x, y) => x - y)

  return {
    status: 'ok',
    bbox: {
      west: Number(ww.toFixed(6)),
      south: Number(ss.toFixed(6)),
      east: Number(ee.toFixed(6)),
      north: Number(nn.toFixed(6)),
    },
    cells,
    evaluated,
    no_data: cells - evaluated,
    area_km2: round3(areaKm2),
    bbox_km2: round3(boxKm2(ww, ss, ee, nn)),
    mean: round3(sum / evaluated),
    median: round3(percentile(values, 0.5)),
    p90: round3(percentile(values, 0.9)),
    max: round3(max),
    max_at: maxAt,
    // 占比的分母是「参与评价的面积」，不是格子数——水体和市域外不该算进分母
    levels: m.levels.map((lv, i) => ({
      name: lv.name,
      color: lv.color,
      km2: Number(areaByLevel[i].toFixed(3)),
      pct: Number(((areaByLevel[i] / areaKm2) * 100).toFixed(1)),
    })),
  }
}
