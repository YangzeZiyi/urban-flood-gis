<script setup>
/**
 * App.vue —— 页面根组件
 * ---------------------------------------------------------------------------
 * 布局很简单：上面一条标题栏，下面是撑满的三维场景，场景左上角浮一块状态面板。
 *
 * 这个文件还顺手做了一件重要的事：**调用后端接口**。
 * 打开页面就会请求 /api/health，把结果显示在面板上——
 * 这样一眼就能看出"前端→后端→数据库"这条链路通不通。
 */
import { computed, onMounted, ref } from 'vue'
import CesiumViewer from './components/CesiumViewer.vue'

// health：后端返回的 JSON 对象；null 表示还没拿到
const health = ref(null)
// loading：请求进行中
const loading = ref(false)
// error：出错信息，空字符串表示没错误
const error = ref('')

/**
 * 请求后端健康检查接口。
 * 注意这里写的是相对路径 '/api/health'，不是 http://localhost:8080/...
 * 因为 vite.config.js 里配了代理，Vite 会帮我们转发到 8080。
 * 这样写的好处：将来部署到服务器，不用改代码。
 */
async function checkHealth() {
  loading.value = true
  error.value = ''
  try {
    const res = await fetch('/api/health')
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`)
    }
    health.value = await res.json()
  } catch (e) {
    // 后端没启动时，浏览器的报错是 "Failed to fetch"，对新手不太友好，
    // 这里翻译成人话
    error.value = `连不上后端（${e.message}）。检查 IDEA 里后端是否在跑，端口 8080。`
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
  if (error.value) return '后端离线'
  if (health.value?.database === 'UP') return '全链路正常'
  if (health.value) return '后端在线 · 数据库异常'
  return '未知'
})

const statusClass = computed(() => {
  if (loading.value) return 'is-loading'
  if (error.value) return 'is-bad'
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
          <li><span>后端服务</span><b>{{ health?.status ?? '—' }}</b></li>
          <li><span>数据库</span><b>{{ health?.database ?? '—' }}</b></li>
          <li><span>PostGIS</span><b>{{ postgisShort }}</b></li>
          <li><span>易涝区</span><b>{{ health?.zoneCount ?? '—' }} 条</b></li>
        </ul>
        <p v-if="error" class="err">{{ error }}</p>
        <p v-else class="tip">拖动旋转 · 滚轮缩放 · 右键平移</p>
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
</style>
