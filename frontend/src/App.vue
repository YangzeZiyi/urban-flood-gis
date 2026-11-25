<script setup>
/**
 * App.vue —— 页面根组件
 * ---------------------------------------------------------------------------
 * 布局很简单：上面一条标题栏，下面是撑满的三维场景，场景左上角浮一块状态面板。
 *
 * 这个文件还顺手做了一件重要的事：**检测数据链路**。
 * 打开页面就问一次"数据在不在"，把结果显示在面板上——
 * 本机模式下这条链路是 前端→后端→数据库，静态版则是 前端→导出好的文件。
 * 两种情况的差异被 api.js 吸收掉了，这里只调 loadHealth()。
 */
import { computed, onMounted, ref } from 'vue'
import CesiumViewer from './components/CesiumViewer.vue'
import { loadHealth, isStatic } from './api.js'

// health：返回的 JSON 对象；null 表示还没拿到
const health = ref(null)
// loading：请求进行中
const loading = ref(false)
// error：出错信息，空字符串表示没错误
const error = ref('')

/**
 * 检测数据是否可用。
 * 注意这里没有写死 '/api/health'——地址由 api.js 按构建模式决定：
 *   npm run dev / build        → /api/health（Vite 代理转到 8080）
 *   npm run build:static       → /static-data/health.json
 */
async function checkHealth() {
  loading.value = true
  error.value = ''
  try {
    health.value = await loadHealth()
  } catch (e) {
    // 后端没启动时浏览器只报 "Failed to fetch"，对新手不友好，翻译成人话
    error.value = isStatic
      ? `静态数据读不到（${e.message}）。检查 dist/static-data 是否完整。`
      : `连不上后端（${e.message}）。检查 IDEA 里后端是否在跑，端口 8080。`
    health.value = null
  } finally {
    loading.value = false
  }
}

// 页面一加载就查一次
onMounted(checkHealth)

// computed：依赖 health 自动计算，health 变了它会自动重算
const statusText = computed(() => {
  if (loading.value) return '检测中…'
  if (error.value) return isStatic ? '数据缺失' : '后端离线'
  if (health.value?.mode === 'static') return '静态快照 · 在线'
  if (health.value?.database === 'UP') return '全链路正常'
  if (health.value) return '后端在线 · 数据库异常'
  return '未知'
})

const statusClass = computed(() => {
  if (loading.value) return 'is-loading'
  if (error.value) return 'is-bad'
  if (health.value?.mode === 'static') return 'is-ok'
  if (health.value?.database === 'UP') return 'is-ok'
  return 'is-warn'
})

// PostGIS 版本字符串很长（3.7 USE_GEOS=1 USE_PROJ=1 ...），只留前面版本号
const postgisShort = computed(() => {
  const v = health.value?.postgis
  return v ? String(v).split(' ')[0] : '—'
})
</script>

<template>
  <div class="layout">
    <!-- 顶部标题栏 -->
    <header class="topbar">
      <div class="brand">
        <span class="logo">◈</span>
        <div class="titles">
          <h1>城市内涝风险可视化平台</h1>
          <p>Urban Flood Risk Platform · 武汉试点区</p>
        </div>
      </div>

      <!-- 点一下可以手动重新检测 -->
      <button class="status" :class="statusClass" :disabled="loading" @click="checkHealth">
        <i class="dot"></i>
        <span>{{ statusText }}</span>
      </button>
    </header>

    <!-- 三维场景 -->
    <main class="stage">
      <CesiumViewer />

      <!-- 浮层：链路状态 -->
      <aside class="panel">
        <h2>系统连通性</h2>
        <ul>
          <li><span>服务状态</span><b>{{ health?.status ?? '—' }}</b></li>
          <!-- 静态版没有数据库，这两行显示出来只会是一串「—」，干脆换掉 -->
          <template v-if="isStatic">
            <li><span>数据来源</span><b>静态快照</b></li>
          </template>
          <template v-else>
            <li><span>数据库</span><b>{{ health?.database ?? '—' }}</b></li>
            <li><span>PostGIS</span><b>{{ postgisShort }}</b></li>
          </template>
          <li><span>易涝区</span><b>{{ health?.zoneCount ?? '—' }} 条</b></li>
          <li v-if="health?.dataset"><span>数据集</span><b>{{ health.dataset }}</b></li>
        </ul>
        <p v-if="error" class="err">{{ error }}</p>
        <template v-else>
          <p class="tip">拖动旋转 · 滚轮缩放 · 右键平移</p>
          <!-- 这张图是"地形 + 降雨"推出来的易涝可能性，不是实测积水深度，
               也不是防汛指令。写清楚，比让人误读成预警地图要好。 -->
          <p class="tip note">数据仅用于技术演示，不作防汛决策依据</p>
        </template>
      </aside>
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  flex-direction: column;
  height: 100%;
}

/* ---------- 顶栏 ---------- */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  height: 60px;
  flex: none;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  backdrop-filter: blur(8px); /* 毛玻璃 */
  z-index: 10;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo {
  font-size: 22px;
  color: var(--accent);
}

.titles h1 {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.5px;
}

.titles p {
  font-size: 11px;
  color: var(--text-dim);
  letter-spacing: 1px;
  margin-top: 2px;
}

/* ---------- 状态按钮 ---------- */
.status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--border);
  border-radius: 20px;
  cursor: pointer;
  transition: background 0.2s;
}

.status:hover {
  background: rgba(255, 255, 255, 0.09);
}

.status:disabled {
  cursor: default;
  opacity: 0.7;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-dim);
}

.is-ok .dot { background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.is-warn .dot { background: var(--warn); }
.is-bad .dot { background: var(--bad); box-shadow: 0 0 8px var(--bad); }
.is-loading .dot { background: var(--accent); animation: blink 1s infinite; }

@keyframes blink {
  50% { opacity: 0.2; }
}

/* ---------- 场景 ---------- */
.stage {
  position: relative;
  flex: 1;
  min-height: 0; /* flex 子项必须写这个，否则内部 100% 高度会溢出 */
}

/* ---------- 浮层面板 ---------- */
.panel {
  position: absolute;
  top: 16px;
  left: 16px;
  width: 232px;
  padding: 14px 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  backdrop-filter: blur(10px);
  font-size: 13px;
}

.panel h2 {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  letter-spacing: 1px;
  margin-bottom: 10px;
}

.panel ul {
  list-style: none;
}

.panel li {
  display: flex;
  justify-content: space-between;
  padding: 5px 0;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.06);
}

.panel li:last-child {
  border-bottom: none;
}

.panel li span {
  color: var(--text-dim);
}

.panel li b {
  color: var(--accent);
  font-weight: 500;
}

.tip {
  margin-top: 10px;
  font-size: 11px;
  color: var(--text-dim);
  line-height: 1.6;
}

.err {
  margin-top: 10px;
  font-size: 11px;
  color: var(--bad);
  line-height: 1.6;
}

/* 免责那句要比操作提示更轻，否则面板会被文字压沉 */
.tip.note {
  margin-top: 4px;
  font-size: 10px;
  opacity: 0.65;
}
</style>
