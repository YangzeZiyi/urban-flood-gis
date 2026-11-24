/**
 * verify-static.mjs —— 校验「静态数据版」和后端给出的是同一份答案
 * ---------------------------------------------------------------------------
 * 静态版把接口换成了文件，代价是「可能算错而没人发现」：
 * 行列号取整差一格、量化区间定错、面积用了常数、分档阈值从别处抄漏一位，
 * 这些都不会报错，界面照常显示，只是数字不对。
 *
 * 所以这个脚本做的事很朴素：直接 import src/api.js（把它切到静态模式），
 * 再拿同一批经纬度去问还活着的后端，逐字段比。
 * 它是唯一能证明「两份实现等价」的东西，改完 export_static.py 就该跑一次。
 *
 * 用法
 * ---------------------------------------------------------------------------
 *   # 1. 后端要活着（它是对照组）
 *   java -jar release/floodgis-demo.jar
 *   # 2. 数据要导出过
 *   cd pipeline && python publish/export_static.py
 *   # 3. 校验
 *   cd frontend && node scripts/verify-static.mjs
 *
 * 退出码非 0 表示有超出容差的差异，可以直接挂进 CI。
 */
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const DATA = join(ROOT, 'public', 'static-data')
const BACKEND = process.env.API_BASE || 'http://localhost:8080'

// ---------------------------------------------------------------------------
// 让 api.js 以为自己在浏览器里：/static-data/xxx 改成读本地文件
// ---------------------------------------------------------------------------
const realFetch = globalThis.fetch
globalThis.fetch = async (url, opts) => {
  if (typeof url === 'string' && url.startsWith('/static-data/')) {
    const buf = readFileSync(join(DATA, url.slice('/static-data/'.length)))
    return {
      ok: true,
      status: 200,
      json: async () => JSON.parse(buf.toString('utf8')),
      arrayBuffer: async () =>
        buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength),
    }
  }
  return realFetch(url, opts)
}

// import.meta.env 是 Vite 的注入，Node 里没有；做一次等价替换再 import
const TMP = join(ROOT, '.api-static-under-test.mjs')
writeFileSync(TMP, readFileSync(join(ROOT, 'src', 'api.js'), 'utf8')
  .replace(/import\.meta\.env\.VITE_STATIC_DATA/g, "'1'"))
const api = await import(`file://${TMP.replace(/\\/g, '/')}`)
unlinkSync(TMP)

if (!api.isStatic) {
  console.error('api.js 没有切到静态模式，检查上面的替换是否还有效')
  process.exit(1)
}

const meta = JSON.parse(readFileSync(join(DATA, 'grid-meta.json'), 'utf8'))
const [W, S, E, N] = meta.bbox
let failures = 0

// ---------------------------------------------------------------------------
// 1. 单点
// ---------------------------------------------------------------------------
const pts = []
for (let i = 0; i < 300; i++) {
  pts.push([W + Math.random() * (E - W), S + Math.random() * (N - S)])
}
// 再补几个确定的位置：市中心、几个已知易涝点、一个肯定越界的
pts.push(
  [114.305, 30.593], [114.2716, 30.5844], [114.3621, 30.5204],
  [114.2021, 30.6304], [114.4, 31.5],
)

let exact = 0
let outside = 0
let pointBad = 0
let maxIndexDiff = 0
let maxAccDiff = 0
for (const [lon, lat] of pts) {
  const a = await api.queryFloodIndex(lon, lat)
  const b = await realFetch(`${BACKEND}/api/flood-index?lon=${lon.toFixed(6)}&lat=${lat.toFixed(6)}`)
    .then((r) => r.json())
  if (a.status !== b.status) {
    pointBad++
    console.log(`  [状态] ${lon.toFixed(5)},${lat.toFixed(5)} 静态 ${a.status} / 后端 ${b.status}`)
    continue
  }
  if (a.status !== 'ok') { outside++; continue }
  // 行列号必须完全相同——差一格就说明两边的取整公式已经分叉了，这比数值差更严重
  if (a.row !== b.row || a.col !== b.col) {
    pointBad++
    console.log(`  [格子] ${lon.toFixed(5)},${lat.toFixed(5)} 静态 ${a.row},${a.col} / 后端 ${b.row},${b.col}`)
    continue
  }
  if (a.level !== b.level) {
    pointBad++
    console.log(`  [等级] ${lon.toFixed(5)},${lat.toFixed(5)} 静态 ${a.level} / 后端 ${b.level}`)
    continue
  }
  maxIndexDiff = Math.max(maxIndexDiff, Math.abs(a.index - b.index))
  maxAccDiff = Math.max(maxAccDiff, Math.abs(a.acc - b.acc) / Math.max(1, b.acc))
  // 高程、坡度容差取量化步长；索引和等级必须一致
  if (Math.abs(a.index - b.index) > 0.002 || Math.abs(a.elev - b.elev) > 0.2
    || Math.abs(a.slope - b.slope) > 0.02) {
    pointBad++
    console.log(`  [数值] ${lon.toFixed(5)},${lat.toFixed(5)} 静态 ${JSON.stringify(a)} / 后端 ${JSON.stringify(b)}`)
  } else {
    exact++
  }
}
console.log(`\n[单点] 一致 ${exact}，越界一致 ${outside}，不一致 ${pointBad}`
  + `（索引最大差 ${maxIndexDiff.toFixed(4)}，汇流最大相对差 ${(maxAccDiff * 100).toFixed(3)}%）`)
failures += pointBad

// ---------------------------------------------------------------------------
// 2. 框选
// ---------------------------------------------------------------------------
const boxes = [
  ['全市', W, S, E, N],
  ['内环一带', 114.26, 30.55, 114.34, 30.61],
  ['南湖片区', 114.32, 30.47, 114.39, 30.53],
  ['汉口江滩', 114.27, 30.60, 114.31, 30.63],
  ['青山老工业区', 114.38, 30.60, 114.45, 30.65],
  ['200 米小框', 114.3000, 30.5900, 114.3020, 30.5918],
  ['框在市域外', 113.0, 29.0, 113.5, 29.5],
  ['经纬度颠倒', 114.3, 30.6, 114.2, 30.5],
]
// 面积容差 0.5%：量化只会让个别格子换档，不会让面积整体偏移
const AREA_TOL = 0.005
const PCT_TOL = 0.8

console.log('\n[框选]')
for (const [name, w, s, e, n] of boxes) {
  const a = await api.queryFloodStats(w, s, e, n)
  const b = await realFetch(`${BACKEND}/api/flood-index/stats`
    + `?west=${w}&south=${s}&east=${e}&north=${n}`).then((r) => r.json())
  if (a.status !== b.status) {
    console.log(`  ${name}: 状态不一致 静态 ${a.status} / 后端 ${b.status}`)
    failures++
    continue
  }
  if (a.status !== 'ok') {
    console.log(`  ${name}: 两边都是 ${a.status}（${a.message}）`)
    continue
  }
  const problems = []
  if (a.cells !== b.cells) problems.push(`格子数 ${a.cells}≠${b.cells}`)
  if (a.evaluated !== b.evaluated) problems.push(`有效格 ${a.evaluated}≠${b.evaluated}`)
  if (a.no_data !== b.no_data) problems.push(`无数据 ${a.no_data}≠${b.no_data}`)
  const areaDiff = Math.abs(a.area_km2 - b.area_km2) / b.area_km2
  if (areaDiff > AREA_TOL) problems.push(`参与评价面积 ${a.area_km2}≠${b.area_km2}`)
  const meanDiff = Math.abs(a.mean - b.mean)
  if (meanDiff > 0.002) problems.push(`均值 ${a.mean}≠${b.mean}`)
  const pctDiff = Math.max(...a.levels.map((lv, i) => Math.abs(lv.pct - b.levels[i].pct)))
  if (pctDiff > PCT_TOL) problems.push(`分档占比最大差 ${pctDiff.toFixed(2)}pp`)

  console.log(`  ${name}: ${problems.length ? '✗ ' + problems.join('，') : '✓'}`
    + `  面积 ${a.area_km2.toFixed(1)}/${b.area_km2.toFixed(1)} km²`
    + `  均值 ${a.mean.toFixed(3)}/${b.mean.toFixed(3)}`
    + `  分档 ${a.levels.map((lv) => lv.pct.toFixed(1)).join('/')}`
    + ` vs ${b.levels.map((lv) => lv.pct.toFixed(1)).join('/')}`)
  if (problems.length) failures++
}

// ---------------------------------------------------------------------------
// 3. 状态与地址
// ---------------------------------------------------------------------------
const health = await api.loadHealth()
console.log('\n[health]', JSON.stringify(health))
if (health.mode !== 'static') {
  console.log('  health.json 没有标成静态快照')
  failures++
}
for (const [name, url] of [['zones', api.zonesUrl()], ['admin', api.adminUrl()]]) {
  const buf = readFileSync(join(DATA, url.slice('/static-data/'.length)))
  const gj = JSON.parse(buf.toString('utf8'))
  console.log(`[${name}] ${url}  features=${gj.features.length}`)
  if (!gj.features.length) failures++
}

console.log(`\n${failures ? `✗ ${failures} 项不通过` : '✓ 全部通过：静态版与后端逐格一致'}`)
process.exit(failures ? 1 : 0)
