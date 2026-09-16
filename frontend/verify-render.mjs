/**
 * 组件渲染自检（临时脚本）。
 *
 * 用 Vue 的 SSR 渲染器把 App.vue 真正实例化并渲染成 HTML：
 * 走的是和浏览器完全相同的模板编译产物与 setup 执行路径，
 * 因此能证明「组件没有运行时错误、结构确实渲染出来了」——
 * 这比截图可靠（截图受渲染时机/视口影响，空白图无法区分是页面坏了还是截图坏了）。
 *
 * 局限（如实说明）：SSR 下 onMounted 不执行，所以不发任何请求；
 * 交互与网络行为由 verify-sse.mjs 覆盖。
 *   node _ssr_check.mjs
 */
import { createServer } from 'vite'
// 必须用 Node 原生 ESM 导入 vue：走 ssrLoadModule('vue') 会加载到 CJS 入口
// （vue/index.js），在 ESM 下直接 `ReferenceError: module is not defined`。
// Vite 的 SSR 默认把 node_modules 里的依赖外部化，所以组件里的 `import 'vue'`
// 解析到的就是这个同一实例，不会出现双实例问题。
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  // ssrLoadModule 会触发依赖预扫描，而 dep-scan 走 esbuild、不认 .vue 扩展，
  // 会在 TracePanel → ToolPanel 这种组件间导入上直接报错。关掉发现即可 ——
  // 预打包只是 dev 期优化，对本次渲染验证没有影响。
  optimizeDeps: { noDiscovery: true, include: [] },
  logLevel: 'error',
})

let fail = 0
const ok = (label, cond, detail = '') => {
  if (!cond) fail += 1
  console.log(`  [${cond ? '✓' : '✗'}] ${label}${detail ? '  ' + detail : ''}`)
}

try {
  const { default: App } = await vite.ssrLoadModule('/src/App.vue')

  const html = await renderToString(createSSRApp(App))

  console.log(`渲染完成：${html.length} 字节\n`)
  ok('顶栏渲染', html.includes('topbar') && html.includes('商枢 CommercePivot'))
  ok('连接状态徽章渲染', html.includes('连接中') || html.includes('后端未连接'))
  ok('空状态卡片渲染', html.includes('开始验证') && html.includes('上个月各平台的销售额分别是多少？'))
  ok('输入区渲染', html.includes('composer__input') && html.includes('流式 SSE'))
  ok('右侧链路面板渲染', html.includes('链路概览') && html.includes('MCP 工具'))
  ok('内联品牌图标渲染', html.includes('<svg class="topbar__logo"'))
  ok('无 Vue 报错占位', !html.includes('__VUE_ERROR__'))

  console.log('\n--- 渲染片段（前 320 字符）---')
  console.log(html.replace(/\s+/g, ' ').slice(0, 320))
} catch (e) {
  fail += 1
  console.log(`  [✗] 渲染抛错：${e?.stack || e}`)
} finally {
  await vite.close()
}

console.log(`\n${fail ? `存在 ${fail} 项失败` : '全部通过'}`)
process.exit(fail ? 1 : 0)
