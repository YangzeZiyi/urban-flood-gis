/**
 * 前端入口文件
 * ---------------------------------------------------------------------------
 * 整个前端从这一行代码开始跑。Vite 会把它当作打包入口，顺着 import 一路
 * 找出所有依赖（App.vue、样式、Cesium...）打成一个包。
 */
import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

// createApp(根组件).mount(容器选择器)
// 把 App.vue 渲染出来，塞进 index.html 里那个 <div id="app"></div>
createApp(App).mount('#app')
