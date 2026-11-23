/**
 * 把 Cesium 的"静态资源"拷到 public/cesium/ 下
 * ---------------------------------------------------------------------------
 * 为什么需要这一步？
 *
 * Cesium 除了 JS 代码，还带一批"运行时才去加载"的非 JS 文件：
 *   - Assets/    贴图、地形瓦片、图标（比如离线的天然地球底图）
 *   - Widgets/   控件的 CSS 和图片
 *   - Workers/   Web Worker 脚本（几何计算放在子线程，避免卡住界面）
 *   - ThirdParty/ 第三方依赖
 *
 * 这些文件不会被 Vite 打包（打包器只处理代码），所以必须原样放到 public/ 目录下，
 * 浏览器按 CESIUM_BASE_URL 去找。public/ 里的文件会被原封不动复制到构建产物根目录。
 *
 * 为什么要写成脚本，而不是手动复制？
 *   1. 换电脑、重装 node_modules 后，npm install 会自动再跑一次（postinstall 钩子）
 *   2. 手动复制容易漏目录，漏了 Workers 就会出现"球转不动、报 404"的怪问题
 * ---------------------------------------------------------------------------
 */
import { cp, mkdir, rm, access } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const root = join(__dirname, '..')

const SRC = join(root, 'node_modules', 'cesium', 'Build', 'Cesium')
const DEST = join(root, 'public', 'cesium')
const DIRS = ['Assets', 'Widgets', 'Workers', 'ThirdParty']

async function exists(p) {
  try {
    await access(p)
    return true
  } catch {
    return false
  }
}

if (!(await exists(SRC))) {
  console.error('[copy-cesium] 找不到 cesium 包，请先执行 npm install')
  process.exit(0) // 不让安装流程整体失败
}

// 先清空旧目录，避免版本升级后残留旧文件
await rm(DEST, { recursive: true, force: true })
await mkdir(DEST, { recursive: true })

for (const dir of DIRS) {
  const from = join(SRC, dir)
  if (!(await exists(from))) continue
  await cp(from, join(DEST, dir), { recursive: true })
  console.log(`[copy-cesium] ${dir} -> public/cesium/${dir}`)
}

console.log('[copy-cesium] 完成，Cesium 静态资源已就位')
