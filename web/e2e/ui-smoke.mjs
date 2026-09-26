/**
 * 浏览器冒烟验证：真的开一个浏览器，打开工作台，等数据渲染出来再截图。
 *
 * 为什么需要它——HTTP 层测试全绿不代表界面能看。M7 就踩过一次：
 * 接口 200、响应 20ms，但页面白屏。抓下来才看到两个只有真浏览器才暴露的问题：
 *   1. JS 以 text/plain 下发，浏览器拒绝执行 ES module（Windows 注册表的锅）
 *   2. 三个请求用 Promise.all + catch 吞错，任一失败则整屏显示"没有数据"
 * 这两类问题 TestClient 永远测不出来，所以留一个"真开浏览器"的验证入口。
 *
 * 脚本自己拉起 Chrome（独立 profile + 随机调试端口），跑完关掉，
 * 不需要事先手动起浏览器，也不会污染本机 Chrome 配置。
 *
 * 用法（先让服务跑起来）：
 *   node web/e2e/ui-smoke.mjs                       # 默认打 http://127.0.0.1:8000/
 *   TARGET=http://127.0.0.1:8011/ OUT=shot.png node web/e2e/ui-smoke.mjs
 *   OPEN_FIRST_ROW=1 node web/e2e/ui-smoke.mjs      # 展开第一条候选人看证据区
 *
 * 环境变量：
 *   TARGET         页面地址，默认 http://127.0.0.1:8000/
 *   OUT            截图输出路径，默认 web/e2e/ui-smoke.png
 *   WIDTH/HEIGHT   视口尺寸，默认 1500x1150
 *   OPEN_FIRST_ROW 置 1 则展开第一条，用于检查原文引用是否渲染
 *   CHROME         Chrome 可执行文件路径（默认自动在常见位置找）
 *   EXPECT_ROWS    期望的候选人条数；设了就对不上则退出码 1（可当断言用）
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'

const TARGET = process.env.TARGET || 'http://127.0.0.1:8000/'
const OUT = process.env.OUT || 'web/e2e/ui-smoke.png'
const WIDTH = Number(process.env.WIDTH || 1500)
const HEIGHT = Number(process.env.HEIGHT || 1150)
const OPEN_FIRST_ROW = process.env.OPEN_FIRST_ROW === '1'
const EXPECT_ROWS = process.env.EXPECT_ROWS ? Number(process.env.EXPECT_ROWS) : null

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const CHROME_CANDIDATES = [
  process.env.CHROME,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`,
  `${process.env.LOCALAPPDATA}/Microsoft/Edge/Application/msedge.exe`,
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean)

function findChrome() {
  for (const candidate of CHROME_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate
  }
  throw new Error(
    '找不到 Chrome。请设置 CHROME 环境变量指向浏览器可执行文件，\n' +
      `已尝试：\n  ${CHROME_CANDIDATES.join('\n  ')}`,
  )
}

async function freePort() {
  const net = await import('node:net')
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

/** 连上 CDP 并拿一个可用的 page target。 */
async function connect(port) {
  let page = null
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      page = list.find((item) => item.type === 'page')
      if (page) break
    } catch {
      /* Chrome 还没起来，继续等 */
    }
    await sleep(250)
  }
  if (!page) throw new Error('无法连接 CDP，Chrome 可能没起来')

  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    ws.onopen = resolve
    ws.onerror = reject
  })

  let seq = 0
  const waiters = new Map()
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data)
    const waiter = waiters.get(msg.id)
    if (waiter) {
      waiters.delete(msg.id)
      waiter(msg)
    }
  }
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++seq
      waiters.set(id, (msg) =>
        msg.error ? reject(new Error(`${method}: ${JSON.stringify(msg.error)}`)) : resolve(msg.result),
      )
      ws.send(JSON.stringify({ id, method, params }))
    })

  return { send, close: () => ws.close() }
}

/** 探针表达式：页面上关键节点是否已经渲染出来。 */
const PROBE =
  'JSON.stringify({' +
  'rows: document.querySelectorAll(".row").length,' +
  'groups: document.querySelectorAll(".group").length,' +
  'jobs: document.querySelectorAll(".job-item").length,' +
  'emptyRanking: !!document.querySelector(".empty-title"),' +
  'err: (document.querySelector(".banner-error") || {}).textContent || ""' +
  '})'

async function main() {
  const chrome = findChrome()
  const port = await freePort()
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ui-smoke-'))

  console.log(`浏览器：${chrome}`)
  console.log(`调试端口：${port}`)

  const proc = spawn(
    chrome,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      `--window-size=${WIDTH},${HEIGHT}`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  )

  let failed = false
  try {
    const { send, close } = await connect(port)

    await send('Emulation.setDeviceMetricsOverride', {
      width: WIDTH,
      height: HEIGHT,
      deviceScaleFactor: 1,
      mobile: false,
    })
    await send('Page.enable')
    await send('Page.navigate', { url: TARGET })

    // 轮询到"渲染出来"或"报错"为止——直接 --screenshot 会在数据到达前就截，
    // 拿到一张"空界面"，然后让人去查一个根本不存在的 bug。
    let state = null
    for (let attempt = 0; attempt < 80; attempt += 1) {
      await sleep(250)
      const result = await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })
      state = JSON.parse(result.result.value)
      if (state.rows > 0 || state.err) break
    }
    console.log('页面状态：', JSON.stringify(state))

    if (OPEN_FIRST_ROW) {
      await send('Runtime.evaluate', {
        expression: 'document.querySelector(".row-toggle")?.click()',
        returnByValue: true,
      })
      await sleep(400)
    }

    await sleep(400)
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
    fs.mkdirSync(path.dirname(OUT), { recursive: true })
    fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'))
    console.log(`已保存 ${OUT}（${fs.statSync(OUT).size} 字节）`)

    close()

    if (state.err) {
      console.error(`页面出现错误横幅：${state.err}`)
      failed = true
    }
    if (EXPECT_ROWS !== null && state.rows !== EXPECT_ROWS) {
      console.error(`候选人条数不符：期望 ${EXPECT_ROWS}，实际 ${state.rows}`)
      failed = true
    }
  } finally {
    proc.kill()
    await sleep(200)
    fs.rmSync(profile, { recursive: true, force: true })
  }

  process.exit(failed ? 1 : 0)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
