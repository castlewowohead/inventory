/**
 * Isolated H5 smoke for the 2026-11-23 第 11 节 fixes. Run from the repo root:
 *
 *   node backend/tools/smoke_multifiles_s11.mjs
 *
 * Covers, with real clicks in a real browser against a real server:
 *   ① one 上传单据 entry; a one-page PDF skips the board, a 3-page PDF does not
 *   ② 新建日期类别 typed into the fixed 年-月-日 boxes
 *   ③ 编辑中 → 撤回填表（卡片上的红按钮，第 13 节 ②）clears the draft and keeps the classification
 *   ④ 继续填表 gets back into the draft after leaving the fill page
 *   ⑤ no 单据列表 button, and 返回 from a draft lands back on the board
 *
 * 2026-11-24 第 17 节 also rides along at the end: the per-card 改商家, the
 * question asked before a card is merged into another supplier's group, the
 * fill page without its empty 识别进度 card, 来源 with the same green/red pair,
 * and 删除本张单据 taking its pages off the organizing board.
 *
 * Everything (database, uploads, browser profile) lives in a session temp dir
 * and is removed at the end. OCR is the fake provider: no model is called.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import net from 'node:net'
import { spawn, spawnSync } from 'node:child_process'

const root = process.cwd()
const backend = path.join(root, 'backend')
const frontend = path.join(root, 'frontend', 'dist', 'build', 'h5')
const python = path.join(backend, '.venv', 'Scripts', 'python.exe')
const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'inventory-browser-'))
const db = path.join(work, 'smoke.db').replaceAll('\\', '/')
const API = 5321
const WEB = 5322
const CDP_PORT = 9343
const env = { ...process.env, APP_ENV: 'testing', PORT: String(API), HOST: '127.0.0.1',
  OCR_PROVIDER: 'fake', DATABASE_URL: `sqlite:///${db}`,
  FILE_STORAGE_DIR: path.join(work, 'uploads'), SECRET_KEY: 'isolated-browser-smoke' }
const children = []
const findings = {}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const assert = (condition, message) => { if (!condition) throw new Error(message) }

async function requireFreePort(port) {
  await new Promise((resolve, reject) => {
    const probe = net.connect({ host: '127.0.0.1', port })
    probe.once('connect', () => { probe.destroy(); reject(new Error(`Port ${port} is already in use`)) })
    probe.once('error', (error) => {
      probe.destroy()
      error.code === 'ECONNREFUSED' ? resolve() : reject(error)
    })
  })
}
function runPython(args) {
  const result = spawnSync(python, args, { cwd: backend, env, encoding: 'utf8', windowsHide: true })
  if (result.status !== 0) throw new Error(`python ${args[0]} failed: ${result.stderr || result.stdout}`)
  return result.stdout
}
async function waitUntil(check, label, max = 25000) {
  const start = Date.now()
  let last = ''
  while (Date.now() - start < max) {
    try { const result = await check(); if (result) return result } catch (error) { last = error.message }
    await sleep(150)
  }
  throw new Error(`Timed out: ${label}${last ? ` (${last})` : ''}`)
}
function start(command, args, cwd = root) {
  const child = spawn(command, args, { cwd, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  children.push(child)
  child.logs = ''
  for (const stream of [child.stdout, child.stderr]) {
    stream.on('data', (data) => { child.logs = (child.logs + String(data)).slice(-8000) })
  }
  return child
}
function staticServer() {
  // ⚠️ `.mjs` must be served as JavaScript or pdf.js never gets its worker —
  // and this page counts the pages of a local PDF before uploading it.
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
    '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
    '.png': 'image/png', '.woff2': 'font/woff2', '.wasm': 'application/wasm' }
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    if (pathname.startsWith('/api/') || pathname === '/health') {
      const proxy = http.request({ hostname: '127.0.0.1', port: API, method: request.method,
        path: request.url, headers: request.headers }, (incoming) => {
        const legacyCounts = process.env.SMOKE_LEGACY_COUNTS === '1' && request.method === 'GET'
          && /^\/api\/intake-batches\/(?:current|\d+)$/.test(pathname)
          && incoming.statusCode === 200
        if (!legacyCounts) {
          response.writeHead(incoming.statusCode, incoming.headers)
          incoming.pipe(response)
          return
        }
        const chunks = []
        incoming.on('data', (chunk) => chunks.push(chunk))
        incoming.on('end', () => {
          const batch = JSON.parse(Buffer.concat(chunks).toString('utf8'))
          delete batch.item_count
          delete batch.unfinished_item_count
          const body = Buffer.from(JSON.stringify(batch))
          response.writeHead(200, { ...incoming.headers, 'content-length': body.length })
          response.end(body)
        })
      })
      proxy.on('error', (error) => { response.writeHead(502); response.end(error.message) })
      request.pipe(proxy)
      return
    }
    const requested = path.resolve(frontend, `.${decodeURIComponent(pathname)}`)
    const file = requested.startsWith(frontend) && fs.existsSync(requested)
      && fs.statSync(requested).isFile() ? requested : path.join(frontend, 'index.html')
    response.writeHead(200, { 'content-type': types[path.extname(file)] || 'application/octet-stream' })
    fs.createReadStream(file).pipe(response)
  })
  server.listen(WEB, '127.0.0.1')
  return server
}
async function api(url, options = {}) {
  const response = await fetch(`http://127.0.0.1:${API}${url}`, options)
  const data = response.status === 204 ? {} : await response.json()
  if (!options.allowError) {
    assert(response.ok, `${url}: ${response.status} ${JSON.stringify(data)}`)
  }
  return { status: response.status, data }
}
function json(method, data) {
  return { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(data) }
}
/**
 * 一份**带文字层**的合成 PDF：商家与 `6/3/2026` 都印在纸上。
 *
 * 走文字层这条路就不调模型（M05 的分流），所以这一步验的是真的解析，不是 Fake
 * 识别器编出来的值。生成器借用测试里的 `text_pdf`——本仓库没有 PDF 排版库。
 */
function makeDatedPdf(name) {
  const file = path.join(work, name)
  runPython(['-c', "import sys; sys.path.insert(0, '.');"
    + ' from tests.test_intake_recognition import text_pdf;'
    + " page = [(72, 740, 'Vendor: Hudson Street Provisions'),"
    + " (72, 715, 'Date: 6/3/2026'), (72, 690, 'Invoice # HS-014'),"
    + " (72, 640, 'Description'), (260, 640, 'Qty'), (320, 640, 'Unit'),"
    + " (380, 640, 'Unit Price'), (470, 640, 'Amount'),"
    + " (72, 620, 'Salmon'), (260, 620, '3'), (320, 620, 'lb'),"
    + " (380, 620, '9.85'), (470, 620, '29.55')];"
    + ' open(sys.argv[1], "wb").write(text_pdf([page]))', file])
  return file
}

function makePdf(name, pages) {
  const file = path.join(work, name)
  runPython(['-c', 'from pypdf import PdfWriter; import sys; w=PdfWriter();'
    + ' [w.add_blank_page(width=612,height=792) for _ in range(int(sys.argv[2]))]; w.write(sys.argv[1])',
    file, String(pages)])
  return file
}

class Cdp {
  constructor(socket) {
    this.socket = socket
    this.next = 1
    this.pending = new Map()
    socket.addEventListener('message', (event) => {
      const data = JSON.parse(event.data)
      const pending = this.pending.get(data.id)
      if (!pending) return
      this.pending.delete(data.id)
      data.error ? pending.reject(new Error(data.error.message)) : pending.resolve(data.result)
    })
  }
  send(method, params = {}) {
    const id = this.next++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }
  async eval(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) {
      throw new Error(`${expression}: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`)
    }
    return result.result.value
  }
  text() { return this.eval('document.body.innerText') }
  /** Clicks the first button whose label contains `label`; throws if absent. */
  click(label, scope = 'document') {
    return this.eval(`(() => { const target = [...${scope}.querySelectorAll('button,uni-button')]
      .find((node) => node.textContent.includes(${JSON.stringify(label)}));
      if (!target) throw new Error('no button: ' + ${JSON.stringify(label)});
      target.scrollIntoView({ block: 'center' }); target.click(); return true })()`)
  }
  async attachFile(selector, file) {
    const handle = await this.send('Runtime.evaluate', { expression: `document.querySelector('${selector}')` })
    assert(handle.result.objectId, `no file input for ${selector}`)
    await this.send('DOM.setFileInputFiles', { files: [file], objectId: handle.result.objectId })
  }
  async go(url) {
    await this.send('Page.navigate', { url })
    await sleep(600)
  }
}

let server
let cdp
try {
  for (const port of [API, WEB, CDP_PORT]) await requireFreePort(port)
  assert(fs.existsSync(path.join(frontend, 'index.html')), 'build the H5 bundle first')
  runPython(['-m', 'alembic', 'upgrade', 'head'])
  start(python, ['app.py'], backend)
  await waitUntil(async () => (await fetch(`http://127.0.0.1:${API}/health`)).ok, 'Flask')
  const emptyBatch = (await api('/api/intake-batches', json('POST', {}))).data
  assert((await api('/api/intake-batches/current', { allowError: true })).status === 404,
    `an empty open batch must not create a resume entry: ${emptyBatch.id}`)
  server = staticServer()
  await waitUntil(async () => (await fetch(`http://127.0.0.1:${WEB}/`)).ok, 'H5 server')

  const onePage = makePdf('one-invoice.pdf', 1)
  const threePages = makePdf('three-invoices.pdf', 3)

  start(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-allow-origins=*',
    '--remote-debugging-port=' + CDP_PORT, `--user-data-dir=${path.join(work, 'edge')}`,
    '--window-size=1280,900', `http://127.0.0.1:${WEB}/`])
  const target = await waitUntil(async () => {
    const pages = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
    return pages.find((page) => page.type === 'page' && page.url.includes(String(WEB)))
  }, 'Edge CDP')
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  cdp = new Cdp(socket)
  await cdp.send('Runtime.enable')
  await cdp.send('Page.enable')
  await cdp.send('DOM.enable')

  // --- ① 一个上传入口 ------------------------------------------------------
  await waitUntil(async () => (await cdp.text()).includes('上传单据'), 'home')
  const home = await cdp.text()
  assert(!home.includes('混合文件整理') && !home.includes('单张单据上传'),
    `old entries still on the home page: ${home}`)
  findings.home_actions = (await cdp.eval(`[...document.querySelectorAll('.home-action-title')]
    .map((node) => node.textContent)`))

  await cdp.click('上传单据')
  await waitUntil(() => cdp.eval(`!!document.querySelector('input[type=file]')`), 'file picker')
  await cdp.attachFile('input[type=file]', onePage)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'),
    `single page should open the fill page directly, got ${await cdp.eval('location.search')}`)
  const singles = (await api('/api/invoices')).data
  assert(singles.items.length === 1, `one draft from the single document: ${JSON.stringify(singles)}`)
  assert((await api('/api/intake-batches/current', { allowError: true })).status === 404,
    'a single document must not open an organizing batch')
  findings.single_document = { view: 'review', invoice_id: singles.items[0].id, batches: 0 }

  await cdp.go(`http://127.0.0.1:${WEB}/`)
  await waitUntil(async () => (await cdp.text()).includes('上传单据'), 'home again')
  await cdp.click('上传单据')
  await waitUntil(() => cdp.eval(`!!document.querySelector('input[type=file]')`), 'file picker again')
  await cdp.attachFile('input[type=file]', threePages)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=batch'),
    'a three-page PDF goes to the batch')
  const batch = (await waitUntil(async () => {
    const current = await api('/api/intake-batches/current', { allowError: true })
    return current.status === 200 ? current.data : null
  }, 'batch created'))
  const items = await waitUntil(async () => {
    const page = (await api(`/api/intake-batches/${batch.id}/items`)).data
    return page.total === 3 ? page.items : null
  }, 'three pages split')
  findings.multi_page_document = { view: 'batch', batch_id: batch.id, items: items.length }

  // --- ② 固定格式的日期输入 ------------------------------------------------
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('新建日期类别'), 'organizer')
  const boxes = await cdp.eval(`(() => { const field = document.querySelector('.new-date .date-field');
    return { inputs: field.querySelectorAll('input').length, shape: field.innerText.replace(/\\s+/g, ' ').trim() } })()`)
  assert(boxes.inputs === 3 && /YYYY.*MM.*DD/.test(boxes.shape),
    `expected three date boxes, got ${JSON.stringify(boxes)}`)
  assert((await cdp.eval(`[...document.querySelectorAll('.new-date .date-field .dash')]
    .map((node) => node.textContent).join('')`)) === '--', 'the dashes are already there')
  // Type digits only — the user never types a separator.
  await cdp.eval(`(() => { const inputs = [...document.querySelectorAll('.new-date .date-field input')];
    ['2026', '6', '3'].forEach((value, index) => {
      inputs[index].value = value;
      inputs[index].dispatchEvent(new Event('input', { bubbles: true }));
      inputs[index].dispatchEvent(new Event('blur', { bubbles: true }));
    }); return true })()`)
  await cdp.click('新建日期')
  await waitUntil(async () => (await cdp.text()).includes('6 月 3 日'), 'new date category')
  const dateGroups = (await api(`/api/intake-batches/${batch.id}/groups?suggested=true`)).data.groups
  assert(dateGroups.some((group) => group.purchase_date === '2026-06-03'),
    `6 和 3 应当补成 2026-06-03: ${JSON.stringify(dateGroups)}`)
  findings.typed_date = { typed: ['2026', '6', '3'], stored: '2026-06-03' }

  // --- ③④⑤ 编辑中、继续填表、撤回填表 --------------------------------------
  await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'smoke-group', action: 'group',
    items: items.map(({ id, version }) => ({ id, version })),
    purchase_date: '2026-06-03', source_name: 'Smoke Market' }))
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('开始填表'), 'board with the group')
  const toolbar = await cdp.eval(`[...document.querySelector('.head .actions')
    .querySelectorAll('button,uni-button')].map((node) => node.textContent.trim())`)
  assert(!toolbar.some((label) => label.includes('单据列表')),
    `单据列表 button is still there: ${JSON.stringify(toolbar)}`)
  findings.toolbar = toolbar
  // 第 18 节 ①：工具栏上只剩**动作**。「财务表格」是换一种看法，搬到标题旁边的
  // 「▾」里；「重试识别失败项」没有失败项时根本不出现。
  assert(!toolbar.some((label) => label.includes('财务表格')),
    `财务表格 should live in the title menu now: ${JSON.stringify(toolbar)}`)
  assert(!toolbar.some((label) => label.includes('重试识别失败项')),
    `没有失败项时不该有重试按钮：${JSON.stringify(toolbar)}`)
  // 反过来也要验：真有失败项时它必须出现，否则「不出现」是白捡的。
  const sqlite = (sql, ...args) => runPython(['-c',
    'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]);'
    + ' c.execute(sys.argv[2], sys.argv[3:]); c.commit(); c.close()', db, sql, ...args])
  sqlite("update intake_items set recognition_status = 'failed' where id = ?", String(items[1].id))
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  findings.retry_button = await waitUntil(async () => {
    const label = await cdp.eval(`([...document.querySelectorAll('.head .actions button,.head .actions uni-button')]
      .find((node) => node.innerText.includes('重试识别失败项')))?.innerText.trim() ?? ''`)
    return label || null
  }, 'the retry button appears once something failed')
  assert(findings.retry_button.includes('1'), `它该说出有几项：${findings.retry_button}`)
  sqlite("update intake_items set recognition_status = null where id = ?", String(items[1].id))
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => !(await cdp.text()).includes('重试识别失败项'), 'and goes away again')

  // 第 19 节：标题上不写批次号；它留在 URL 和元素属性里。
  findings.head_title = await cdp.eval(`(() => {
    const row = document.querySelector('.head .title-row')
    return { text: row.querySelector('.title').innerText.trim(),
      data_batch_id: row.getAttribute('data-batch-id'), search: location.search }
  })()`)
  assert(findings.head_title.text === '按日期和商家整理',
    `标题上不该有批次号：${JSON.stringify(findings.head_title)}`)
  assert(findings.head_title.data_batch_id === String(batch.id)
    && findings.head_title.search.includes(`batch=${batch.id}`),
    `批次号仍要留在 URL 和属性里：${JSON.stringify(findings.head_title)}`)

  findings.head_lines = await cdp.eval(`[...document.querySelectorAll('.head .muted')]
    .map((node) => node.innerText.trim()).filter((text) => !text.startsWith('待归类'))`)
  assert(findings.head_lines.length === 1 && findings.head_lines[0].startsWith('点「开始/继续识别」'),
    `说明文字只留那一行：${JSON.stringify(findings.head_lines)}`)
  await cdp.eval(`document.querySelector('.head .title-row .caret').click()`)
  findings.view_menu = await waitUntil(async () => {
    const items = await cdp.eval(`[...document.querySelectorAll('.head .view-menu button,.head .view-menu uni-button')]
      .map((node) => node.innerText.trim())`)
    return items.length ? { label: '▾', items } : null
  }, 'the view menu opens')
  assert(findings.view_menu.items.includes('财务表格形式'),
    `标题旁的菜单里该有「财务表格形式」：${JSON.stringify(findings.view_menu)}`)
  // 进去之后，回来的入口在同一个位置（③）。
  await cdp.click('财务表格形式', `document.querySelector('.head .view-menu')`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=ledger'), 'ledger view')
  await waitUntil(() => cdp.eval(`!!document.querySelector('.ledger .heading .title-row .caret')`),
    'the ledger heading')
  const ledgerActions = await cdp.eval(`[...document.querySelectorAll(
    '.ledger .heading .heading-actions button,.ledger .heading .heading-actions uni-button')]
    .map((node) => node.innerText.trim())`)
  await cdp.eval(`document.querySelector('.ledger .heading .title-row .caret').click()`)
  findings.ledger_back = await waitUntil(async () => {
    const items = await cdp.eval(`[...document.querySelectorAll('.ledger .view-menu button,.ledger .view-menu uni-button')]
      .map((node) => node.innerText.trim())`)
    return items.length ? { actions: ledgerActions, items } : null
  }, 'the ledger view menu opens')
  assert(findings.ledger_back.items.includes('卡片整理形式')
    && !findings.ledger_back.actions.some((label) => label.includes('返回卡片整理')),
    `回卡片整理的入口该在左上角标题旁：${JSON.stringify(findings.ledger_back)}`)
  await cdp.click('卡片整理形式', `document.querySelector('.ledger .view-menu')`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=organize'), 'back on the board')

  // 勾选框：看得见的方框是 22px，点击区仍是 44px（第 13 节 ④）。
  findings.checkbox = await cdp.eval(`(() => {
    const pick = [...document.querySelectorAll('[aria-label^="选中"]')]
      .find((node) => node.getAttribute('aria-label').includes('第 1 页'))
    const hit = pick.getBoundingClientRect()
    const box = pick.querySelector('.box').getBoundingClientRect()
    return { hit: Math.round(hit.width), box: Math.round(box.width),
      radius: getComputedStyle(pick.querySelector('.box')).borderRadius }
  })()`)
  assert(findings.checkbox.hit >= 44 && findings.checkbox.box <= 24 && findings.checkbox.box >= 18,
    `勾选框该是小方块 + 44px 点击区：${JSON.stringify(findings.checkbox)}`)

  // ⑤ 单张直接开始填表：不用先勾选，按钮就在「拖动」左边（第 13 节 ⑤）。
  const startButton = `([...document.querySelectorAll('[aria-label^="开始填写"]')]
    .find((node) => node.getAttribute('aria-label').includes('第 1 页')))`
  findings.card_start = await cdp.eval(`(() => {
    const node = ${startButton}
    const grip = node.closest('.side-row').querySelector('.grip').getBoundingClientRect()
    const box = node.getBoundingClientRect()
    return { text: node.innerText.trim(), disabled: node.hasAttribute('disabled'),
      left_of_grip: box.right <= grip.left + 1 && Math.abs(box.top - grip.top) < grip.height,
      background: getComputedStyle(node).backgroundColor }
  })()`)
  assert(findings.card_start.text === '开始填表' && !findings.card_start.disabled
    && findings.card_start.left_of_grip, `卡片上的开始填表：${JSON.stringify(findings.card_start)}`)
  await cdp.eval(`${startButton}.closest('.card').querySelector('.more').click()`)
  findings.card_delete_entry = await waitUntil(async () => cdp.eval(`(() => { const button = [...document.querySelectorAll('.sheet button,.sheet uni-button')]
    .find((node) => node.innerText.trim() === '删除'); return button ? { label: button.innerText.trim(),
    color: getComputedStyle(button).backgroundColor } : null })()`), 'card delete menu')
  assert(findings.card_delete_entry.label === '删除', '卡片「更多」应有删除入口')
  assert(findings.card_delete_entry.color === 'rgb(154, 59, 47)',
    `删除入口应为红色：${JSON.stringify(findings.card_delete_entry)}`)
  await cdp.click('删除', 'document.querySelector(".sheet")')
  findings.card_delete_prompt = await waitUntil(async () => {
    const text = await cdp.eval(`document.querySelector('.sheet')?.innerText || ''`)
    return text.includes('确认删除') ? text : null
  }, 'card delete confirmation')
  assert(findings.card_delete_prompt.includes('这一张内容'), '删除须先向用户确认范围')
  await cdp.click('取消', 'document.querySelector(".sheet")')
  assert((await cdp.text()).includes('已选') === false, '单张开始填表之前不需要勾选任何东西')
  await cdp.eval(`${startButton}.click()`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'), 'fill page')
  const draftId = Number(await cdp.eval(`new URLSearchParams(location.search).get('invoice')`))
  assert(draftId > 0, 'the fill page knows its draft')
  await cdp.go(`http://127.0.0.1:${WEB}/?view=review&invoice=${draftId}`)
  await waitUntil(async () => (await cdp.text()).includes('返回分类页面'), 'return after full reload')
  findings.review_back = await cdp.eval(`([...document.querySelectorAll('button,uni-button')]
    .map((node) => node.innerText.trim()).filter((text) => text.startsWith('← 返回')))`)
  assert(JSON.stringify(findings.review_back) === JSON.stringify(['← 返回分类页面']),
    `批次填表页左上角只能有返回分类页面：${JSON.stringify(findings.review_back)}`)

  // ⑤ 返回 from a draft opened on the board lands back on the board.
  await cdp.click('返回分类页面')
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=organize'),
    `返回 should come back to the board, got ${await cdp.eval('location.search')}`)
  findings.back_from_draft = 'view=organize'

  // ③ the card says 编辑中, not 已送审.
  await waitUntil(async () => (await cdp.text()).includes('编辑中'), 'card marked 编辑中')
  const cardText = await cdp.text()
  assert(!cardText.includes('已送审'), '已送审 wording is gone')

  // ④ back into the draft from the card itself.
  await cdp.click('继续填表')
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'), 'resumed draft')
  assert(Number(await cdp.eval(`new URLSearchParams(location.search).get('invoice')`)) === draftId,
    'resumed the same draft')
  findings.resume_draft = draftId

  // ③ withdraw: the prompt, then the clearing.
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('编辑中'), 'board again')
  // 撤回填表 2026-11-24 起是卡片上的红按钮，不再藏在「更多」里（第 13 节 ②）。
  const withdrawButton = `([...document.querySelectorAll('[aria-label^="撤回"]')]
    .find((node) => node.getAttribute('aria-label').includes('第 1 页')))`
  await waitUntil(async () => cdp.eval(`!!${withdrawButton}`), '卡片上的撤回填表按钮')
  findings.withdraw_button = await cdp.eval(`(() => {
    const node = ${withdrawButton}
    const side = node.closest('.card-side')
    const resume = side.querySelector('.resume').getBoundingClientRect()
    const more = [...side.querySelectorAll('button,uni-button')]
      .find((other) => other.innerText.trim() === '更多').getBoundingClientRect()
    const box = node.getBoundingClientRect()
    return { text: node.innerText.trim(), background: getComputedStyle(node).backgroundColor,
      below_resume: box.top >= resume.bottom - 1,
      more_on_the_right: more.left >= box.right - 1 && Math.abs(more.top - box.top) < box.height }
  })()`)
  assert(findings.withdraw_button.text === '撤回填表', '卡片上要有「撤回填表」')
  assert(findings.withdraw_button.background === 'rgb(154, 59, 47)',
    `撤回填表要是红的，得到 ${findings.withdraw_button.background}`)
  assert(findings.withdraw_button.below_resume, '撤回填表在「继续填表」下面')
  assert(findings.withdraw_button.more_on_the_right, '「更多」在撤回填表右边、同一行')
  // 窄屏也要放得下这两个按钮：卡片正文该让位，而不是把页面撑出横向滚动。
  // 390px 下日期分组与浮动待归类贴片同时可见。
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 2, mobile: true })
  await sleep(400)
  await waitUntil(async () => cdp.eval(`!!${withdrawButton}`), '窄屏上的撤回填表按钮')
  findings.withdraw_button_narrow = await cdp.eval(`(() => {
    const node = ${withdrawButton}
    const box = node.getBoundingClientRect()
    return { visible: box.width > 0 && box.right <= innerWidth + 1,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth }
  })()`)
  assert(findings.withdraw_button_narrow.visible && findings.withdraw_button_narrow.overflow <= 0,
    `390px 放不下撤回填表：${JSON.stringify(findings.withdraw_button_narrow)}`)
  await cdp.send('Emulation.clearDeviceMetricsOverride')
  await sleep(400)
  await cdp.eval(`${withdrawButton}.click()`)
  const prompt = await waitUntil(async () => {
    const text = await cdp.eval(`document.querySelector('.overlay')?.innerText ?? ''`)
    return text.includes('是否继续') ? text : null
  }, 'withdraw prompt')
  findings.withdraw_prompt = prompt.split('\n').filter(Boolean)
  await cdp.click('继续，清空填写数据', `document.querySelector('.overlay')`)
  // ③ 结果留在刚才点的那个对话框里，而不是写在页面顶上看不见的地方。
  findings.withdraw_result = await waitUntil(async () => {
    const text = await cdp.eval(`document.querySelector('.overlay')?.innerText ?? ''`)
    return text.includes('已撤回填表') ? text.replace(/\s+/g, ' ').trim() : null
  }, 'withdraw result in place')
  assert(findings.withdraw_result.includes('行已清空') && findings.withdraw_result.includes('知道了'),
    `撤回结果要说清楚清了什么：${findings.withdraw_result}`)
  await cdp.click('知道了', `document.querySelector('.overlay')`)
  await waitUntil(async () => !(await cdp.eval(`!!document.querySelector('.overlay')`)), 'dialog closed')
  const back = await waitUntil(async () => {
    const page = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .find((item) => item.page_no === 1)
    return page && page.handed_off_at === null ? page : null
  }, 'withdrawn on the server')
  assert(back.decided_purchase_date === '2026-06-03' && back.decided_source_name === 'Smoke Market',
    `classification must survive the withdrawal: ${JSON.stringify(back)}`)
  assert(back.review_invoice_id === null, 'the draft link is gone')
  assert((await api(`/api/invoices/${draftId}`, { allowError: true })).status === 404,
    'the draft itself is gone')
  await waitUntil(async () => !(await cdp.text()).includes('编辑中'), 'card back to 已确认')
  findings.withdrawn = { item_id: back.id, kept: '2026-06-03 / Smoke Market', draft: 404 }

  // ⑥ 多选那条路：跨日期的勾选必须暗下来，并且悬停说得出原因。
  const thirdPage = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
    .find((item) => item.page_no === 3)
  await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'smoke-other-day', action: 'group',
    items: [{ id: thirdPage.id, version: thirdPage.version }],
    purchase_date: '2026-06-18', source_name: 'Smoke Market' }))
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('勾选多个文件填表'), 'the renamed button')
  const pick = (page) => `([...document.querySelectorAll('[aria-label*="选中"]')]
    .find((node) => node.getAttribute('aria-label').includes('第 ${page} 页'))).click()`
  await cdp.eval(pick(1))
  await cdp.eval(pick(3))
  await waitUntil(async () => (await cdp.text()).includes('已选 2 项'), 'two cards across two days')
  const readMultiButtons = () => cdp.eval(`[...document.querySelectorAll('button,uni-button')]
    .filter((node) => node.innerText.trim() === '勾选多个文件填表')
    .map((node) => ({ disabled: node.hasAttribute('disabled'), title: node.getAttribute('title') }))`)
  findings.multi_blocked = await readMultiButtons()
  assert(findings.multi_blocked.length >= 2, `两个日期各有一个按钮：${JSON.stringify(findings.multi_blocked)}`)
  assert(findings.multi_blocked.every((button) => button.disabled
    && button.title === '无法同时入库来自不同日期或者不同来源的文件。'),
    `跨日期勾选要暗下来并说明原因：${JSON.stringify(findings.multi_blocked)}`)

  // 取消跨组的那一张，按钮回到可按，并且走的仍是多选这条路。
  await cdp.eval(pick(3))
  await waitUntil(async () => (await cdp.text()).includes('已选 1 项'), 'back to one card')
  findings.multi_ready = (await readMultiButtons()).filter((button) => !button.disabled).length
  assert(findings.multi_ready === 1, `只有那一组该可按：${JSON.stringify(await readMultiButtons())}`)
  await cdp.eval(`[...document.querySelectorAll('button,uni-button')]
    .find((node) => node.innerText.trim() === '勾选多个文件填表' && !node.hasAttribute('disabled')).click()`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'), 'new draft')
  const refilled = Number(await cdp.eval(`new URLSearchParams(location.search).get('invoice')`))
  findings.refilled_draft = refilled

  // ⑤ 整张单据在这一页里走完：填一行 → 入账 → 完成 → 回到整理页，不经过单据库。
  await waitUntil(async () => (await cdp.text()).includes('加一行'), 'empty draft')
  await cdp.click('加一行')
  await waitUntil(() => cdp.eval(`!!document.querySelector('[aria-label="商品名称"]')`), 'line editor')
  // uni-app wraps the real <input>; the aria-label may sit on the wrapper.
  await cdp.eval(`(() => { const set = (label, value) => {
      const found = document.querySelector('[aria-label="' + label + '"]');
      const node = found.tagName === 'INPUT' ? found : found.querySelector('input');
      node.value = value;
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true })); };
    set('商品名称', 'Pork Spare Rib Cube 靓排粒'); set('数量', '2'); set('采购单位', 'lb');
    set('单价', '3.00'); set('金额', '6.00'); return true })()`)
  await waitUntil(async () => (await cdp.text()).includes('保存这一行'), 'line is dirty')
  await cdp.click('保存这一行')
  await waitUntil(async () => (await cdp.text()).includes('没有未保存的修改'), 'line saved')
  findings.bilingual_food_preview = await cdp.eval(`(() => {
    const read = (label) => { const found = document.querySelector('[aria-label="' + label + '"]');
      return found?.tagName === 'INPUT' ? found.value : found?.querySelector('input')?.value }
    return { name: read('新食材名称'), chinese: read('新食材中文翻译') } })()`)
  assert(findings.bilingual_food_preview.name === 'Pork Spare Rib Cube'
    && findings.bilingual_food_preview.chinese === '靓排粒',
    `新食材应先分开显示中英文供核对：${JSON.stringify(findings.bilingual_food_preview)}`)
  await cdp.eval(`document.querySelector('.category-field .select-button').click()`)
  await waitUntil(() => cdp.eval(`!!document.querySelector('.category-menu .option')`), 'category menu')
  const category = await cdp.eval(`(() => { const option = document.querySelector('.category-menu .option');
    option.click(); return option.textContent.trim() })()`)
  await cdp.click('确认此行并入账')
  await waitUntil(async () => {
    const invoice = (await api(`/api/invoices/${refilled}`)).data
    return invoice.reconciliation.pending_count === 0
  }, `line booked; editor says: ${await cdp.eval(`[...document.querySelectorAll('.error-text,.saved-note')]
    .map((node) => node.innerText).join(' | ')`)}`)
  const bilingualLine = (await api(`/api/invoices/${refilled}`)).data.lines.find((line) => line.description === 'Pork Spare Rib Cube 靓排粒')
  assert(bilingualLine?.food_item?.name === 'Pork Spare Rib Cube'
    && bilingualLine.food_item.name_zh === '靓排粒',
    `入账后食材主名和中文翻译要分开，采购描述要保留原文：${JSON.stringify(bilingualLine?.food_item)}`)
  findings.bilingual_food_saved = { name: bilingualLine.food_item.name, chinese: bilingualLine.food_item.name_zh }
  await cdp.click('检查原件并完成单据')
  await waitUntil(async () => (await cdp.text()).includes('我已检查原件有无漏行'),
    `closeout form; section says: ${await cdp.eval(`document.querySelector('[aria-label="完成单据"]')?.innerText ?? ''`)}`)
  // 第 20 节 ①：点的是**那行字**，不是方框。哑火的勾就发生在这条路上：uni-app 的
  // `label` 在点击目标不是勾选框本身时直接调组件内部的 `_onClick`，**不派发 DOM
  // click**，于是勾看上去打上了、`checkedOriginal` 仍是 false、按钮继续灰着。
  // 两边都要验到：勾之前按钮要是灰的，勾之后要是能按的。
  const completeButton = `[...document.querySelectorAll('button,uni-button')]
    .find((node) => node.textContent.trim() === '完成单据')`
  const checkRow = `document.querySelector('[aria-label="完成单据"] .check-line')`
  const readCheck = () => cdp.eval(`(() => {
    const row = ${checkRow}
    if (!row) throw new Error('no .check-line')
    const box = row.querySelector('.check-box')
    return { checked: row.getAttribute('aria-checked'),
      hit: Math.round(row.getBoundingClientRect().height),
      box: Math.round(box.getBoundingClientRect().width),
      tick: box.innerText.trim(), box_background: getComputedStyle(box).backgroundColor,
      complete_disabled: ${completeButton}.hasAttribute('disabled') } })()`)
  const before = await readCheck()
  // ⚠️ 点完**不能**在同一次 eval 里读结果：Vue 的 DOM 更新是异步的，同一拍读到的
  // 还是旧的属性。分两次读，中间让它刷新完。
  const clicked = await cdp.eval(`(() => {
    const words = [...${checkRow}.querySelectorAll('*')]
      .find((node) => node.textContent.trim() === '我已检查原件有无漏行') ?? ${checkRow}
    words.click(); return words.tagName })()`)
  const after = await waitUntil(async () => {
    const state = await readCheck()
    return state.checked === 'true' ? state : null
  }, '勾上之后那一行要说自己被勾上了')
  findings.closeout_check = { clicked, before, after }
  assert(before.complete_disabled === true,
    `没勾之前「完成单据」就该是灰的：${JSON.stringify(findings.closeout_check)}`)
  // 看得见的那个勾也要量到，不能只验 aria：点击区整行 ≥44px，方框 22px，勾上后填绿。
  assert(after.tick === '✓' && after.box_background === 'rgb(23, 107, 77)'
    && after.box === 22 && after.hit >= 44,
    `勾上之后方框要真的画成绿的：${JSON.stringify(findings.closeout_check)}`)
  assert(after.complete_disabled === false,
    `点那行字要同时勾上并解开按钮：${JSON.stringify(findings.closeout_check)}`)
  // ⚠️ 「检查原件并完成单据」里也含「完成单据」，所以这一下要按整段文字匹配。
  await cdp.eval(`(() => { const target = [...document.querySelectorAll('button,uni-button')]
    .find((node) => node.textContent.trim() === '完成单据');
    if (!target) throw new Error('no 完成单据 button'); target.click(); return true })()`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=organize'),
    `完成单据 should land back on the board, got ${await cdp.eval('location.search')}`)
  const purchases = (await api('/api/purchases')).data
  assert(purchases.items.length === 1 && purchases.items[0].source_name === 'Smoke Market',
    `the purchase keeps the board's classification: ${JSON.stringify(purchases.items)}`)
  findings.completed_on_the_board = { category, after_complete: 'view=organize',
    purchase: { date: purchases.items[0].purchase_date, source: purchases.items[0].source_name } }

  // 完成之后那张卡片不能再说「编辑中」，而且它不再是死胡同：主按钮是蓝色的
  // 「重新填写」（第 20 节 ②）。
  const booked = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
    .find((item) => item.page_no === 1)
  assert(booked.review_status === 'completed', `completed draft state: ${JSON.stringify(booked)}`)
  findings.booked_card = await waitUntil(() => cdp.eval(`(() => {
    const card = [...document.querySelectorAll('.card')]
      .find((node) => node.innerText.includes('重新填写'))
    if (!card) return null
    const button = (text) => [...card.querySelectorAll('button,uni-button')]
      .find((node) => node.textContent.trim() === text)
    const refill = button('重新填写')
    return { label: card.innerText.includes('已完成') ? '已完成' : card.innerText.slice(0, 60),
      refill_background: getComputedStyle(refill).backgroundColor,
      keeps_view_invoice: !!button('查看单据'),
      withdraw_gone: !button('撤回填表'),
      says_editing: card.innerText.includes('编辑中') } })()`),
    'completed card offers 重新填写')
  assert(findings.booked_card.label === '已完成' && !findings.booked_card.says_editing,
    `卡片该标「已完成」：${JSON.stringify(findings.booked_card)}`)
  assert(findings.booked_card.refill_background === 'rgb(31, 59, 99)'
    && findings.booked_card.keeps_view_invoice && findings.booked_card.withdraw_gone,
    `蓝按钮 + 留着查看单据 + 不给撤回填表：${JSON.stringify(findings.booked_card)}`)
  assert(!(await cdp.text()).includes('编辑中'), '编辑中 must not survive the booking')

  // ② 重新填写：问一句 → 清空并撤销入账 → 落进那张新的空表。
  await cdp.click('重新填写')
  await waitUntil(async () => (await cdp.text()).includes('确认清空数据，重新填写吗？'), 'refill dialog')
  findings.refill_dialog = await cdp.eval(
    `document.querySelector('[aria-label="重新填写"]').innerText.replace(/\s+/g, ' ').trim()`)
  await cdp.click('继续，清空并重新填写')
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'),
    `重新填写 should land in the new form, got ${await cdp.eval('location.search')}`)
  const started = Number(await cdp.eval(`new URLSearchParams(location.search).get('invoice')`))
  const afterRefill = (await api(`/api/invoices/${started}`)).data
  const refillCard = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
    .find((item) => item.page_no === 1)
  findings.refilled = {
    new_invoice: started, was: booked.review_invoice_id,
    lines: afterRefill.lines.length, status: afterRefill.status,
    purchases_left: (await api('/api/purchases')).data.items.length,
    card: { review_status: refillCard.review_status, review_invoice_id: refillCard.review_invoice_id,
      date: refillCard.decided_purchase_date, source: refillCard.decided_source_name } }
  assert(started !== booked.review_invoice_id && afterRefill.lines.length === 0,
    `新草稿要是另一张、而且是空的：${JSON.stringify(findings.refilled)}`)
  // 不撤入账就会重复记价：同一张纸填两遍，价格历史里就会出现两条。
  assert(findings.refilled.purchases_left === 0,
    `已入账的采购记录要跟着作废：${JSON.stringify(findings.refilled)}`)
  assert(refillCard.review_status === 'draft' && refillCard.decided_purchase_date === '2026-06-03'
    && refillCard.decided_source_name === 'Smoke Market',
    `日期和商家要原样留着：${JSON.stringify(findings.refilled)}`)
  await cdp.click('返回分类页面')
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=organize'), 'back to the board')

  // --- 第 14 节：传错的文件从上传页撤下来 ----------------------------------
  await cdp.click('返回上传')
  await waitUntil(async () => (await cdp.text()).includes('已保存的文件'), 'upload page')
  // 第 22 节 ③：文件传完了之后，这一页上要做的下一件事是「开始填表」，所以它是绿的；
  // 「继续选择文件」退成白色。原来那个名字说的是下一页叫什么，不是点它要干什么。
  findings.upload_actions = await cdp.eval(`[...document.querySelector('.batch-panel .actions')
    .querySelectorAll('button,uni-button')].map((node) => ({ text: node.innerText.trim(),
      background: getComputedStyle(node).backgroundColor }))`)
  {
    const start = findings.upload_actions.find((item) => item.text === '开始填表')
    const choose = findings.upload_actions.find((item) => item.text === '继续选择文件')
    assert(start && start.background === 'rgb(23, 107, 77)',
      `上传页的主按钮该是绿色的「开始填表」：${JSON.stringify(findings.upload_actions)}`)
    assert(choose && choose.background === 'rgb(255, 255, 255)',
      `「继续选择文件」该退成白色：${JSON.stringify(findings.upload_actions)}`)
    assert(!findings.upload_actions.some((item) => item.text === '按日期和商家整理'),
      `旧名字不该还在：${JSON.stringify(findings.upload_actions)}`)
  }
  const savedRows = () => cdp.eval(`[...document.querySelectorAll('.files.saved .file-row')].map((row) => {
    const button = [...row.querySelectorAll('button,uni-button')]
      .find((node) => node.innerText.trim() === '移除')
    return { name: row.querySelector('.name').textContent.trim(),
      text: row.innerText.replace(/\s+/g, ' ').trim(),
      remove: button ? { disabled: button.hasAttribute('disabled'),
        title: button.getAttribute('title') } : null } })`)
  // 这一批的第 1 页刚刚入库，所以它那份文件现在撤不掉——按钮要自己暗着并说出原因。
  findings.remove_blocked = (await savedRows())[0]
  assert(findings.remove_blocked.remove && findings.remove_blocked.remove.disabled,
    `已开始填表的那份文件不该能移除：${JSON.stringify(findings.remove_blocked)}`)
  assert(findings.remove_blocked.remove.title.includes('撤回填表')
    && findings.remove_blocked.text.includes('有 1 项'),
    `移除按钮要说得出原因：${JSON.stringify(findings.remove_blocked)}`)

  // 传错一份，再把它撤下来。
  const wrongFile = makePdf('wrong-upload.pdf', 1)
  // ⚠️ uni-app 的 chooseFile 会留着上一次那个隐藏 input，所以先把现有的标记掉，
  // 再认「这一次新建出来的那个」——把旧的删掉会让第二次点击根本建不出新的。
  await cdp.eval(`[...document.querySelectorAll('input[type=file]')]
    .forEach((node) => node.setAttribute('data-used', '1')); true`)
  await cdp.click('继续选择文件')
  await waitUntil(() => cdp.eval(`!!document.querySelector('input[type=file]:not([data-used])')`),
    'batch file picker')
  await cdp.attachFile('input[type=file]:not([data-used])', wrongFile)
  await waitUntil(async () => (await savedRows()).length === 2, 'the wrong file is saved')
  const wrongRow = (await savedRows()).find((row) => row.name === 'wrong-upload.pdf')
  assert(wrongRow && wrongRow.remove && !wrongRow.remove.disabled,
    `刚传进来的文件该能移除：${JSON.stringify(wrongRow)}`)
  await cdp.eval(`document.querySelector('[aria-label="移除 wrong-upload.pdf"]').click()`)
  findings.remove_prompt = (await waitUntil(async () => {
    const text = await cdp.eval(`document.querySelector('.overlay')?.innerText ?? ''`)
    return text.includes('移除') ? text.replace(/\s+/g, ' ').trim() : null
  }, 'remove prompt'))
  assert(findings.remove_prompt.includes('不能反悔')
    && findings.remove_prompt.includes('全新的一份'),
    `对话框要说清楚这一步删掉什么、不能反悔（第 16 节）：${findings.remove_prompt}`)
  await cdp.click('移除这份文件', `document.querySelector('.overlay')`)
  await waitUntil(async () => !(await cdp.eval(`!!document.querySelector('.overlay')`)), 'remove done')
  await waitUntil(async () => (await savedRows()).length === 1, 'the wrong file is gone from the list')
  findings.removed_notice = await cdp.eval(`[...document.querySelectorAll('.files text,.files uni-text')]
    .map((node) => node.innerText).find((text) => text.includes('已移除')) ?? ''`)
  assert(findings.removed_notice.includes('wrong-upload.pdf')
    && findings.removed_notice.includes('全新的一份'),
    `移除之后要在文件列表这里说一句：${findings.removed_notice}`)
  // 「本次选择」里那一条也跟着走：刚传上去又撤下来的文件不该还写着「已保存」。
  findings.removed_from_the_local_list = await cdp.eval(`[...document
    .querySelectorAll('.files.chosen .name')].map((node) => node.textContent.trim())`)
  assert(!findings.removed_from_the_local_list.includes('wrong-upload.pdf'),
    `本地那一条也要撤掉：${JSON.stringify(findings.removed_from_the_local_list)}`)
  const afterRemoval = (await api(`/api/intake-batches/${batch.id}`)).data
  assert(afterRemoval.files.length === 1 && afterRemoval.files[0].original_name === 'three-invoices.pdf',
    `服务端也该只剩那一份：${JSON.stringify(afterRemoval.files.map((file) => file.original_name))}`)
  assert((await api(`/api/intake-batches/${batch.id}/items`)).data.total === 3,
    '撤掉的那一页不再出现在整理页，原来的三页一个不少')
  findings.removed = { left: afterRemoval.files.map((file) => file.original_name),
    items: (await api(`/api/intake-batches/${batch.id}/items`)).data.total }

  // 第 16 节：真删。同一份文件再传回来是全新的一份，旧分类不跟着回来。
  const beforeRetry = (await api(`/api/intake-batches/${batch.id}/items`)).data.total
  await cdp.eval(`[...document.querySelectorAll('input[type=file]')]
    .forEach((node) => node.setAttribute('data-used', '1')); true`)
  await cdp.click('继续选择文件')
  await waitUntil(() => cdp.eval(`!!document.querySelector('input[type=file]:not([data-used])')`),
    'picker for the re-upload')
  await cdp.attachFile('input[type=file]:not([data-used])', wrongFile)
  const reUploaded = await waitUntil(async () => {
    const rows = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .filter((row) => row.original_name === 'wrong-upload.pdf')
    return rows.length ? rows : null
  }, 'the same file comes back as a new one')
  findings.re_uploaded_clean = {
    items: reUploaded.length,
    decided: reUploaded.map((row) => row.decided_purchase_date),
    total_before: beforeRetry,
  }
  assert(reUploaded.every((row) => row.decided_purchase_date === null
    && row.decided_source_name === null && row.decided_at === null),
    `重传回来的那一份不该带着旧分类：${JSON.stringify(findings.re_uploaded_clean)}`)
  // 收拾干净，后面的断言仍按一份文件算。
  const retryRef = (await api(`/api/intake-batches/${batch.id}`)).data.files
    .find((file) => file.original_name === 'wrong-upload.pdf')
  await api(`/api/intake-batches/${batch.id}/files/${retryRef.id}`, { method: 'DELETE' })

  // --- 第 15 节：6/3 就是 6 月 3 日，不再问「这是几月」 --------------------
  const datedPdf = makeDatedPdf('dated-invoice.pdf')
  await cdp.eval(`[...document.querySelectorAll('input[type=file]')]
    .forEach((node) => node.setAttribute('data-used', '1')); true`)
  await cdp.click('继续选择文件')
  await waitUntil(() => cdp.eval(`!!document.querySelector('input[type=file]:not([data-used])')`),
    'picker again')
  await cdp.attachFile('input[type=file]:not([data-used])', datedPdf)
  const datedItem = await waitUntil(async () => {
    const page = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .find((row) => row.original_name === 'dated-invoice.pdf')
    return page ?? null
  }, 'the dated page is split out')
  // 文字层这条路不调模型；`model_calls` 是服务端自己报的。
  const recognized = (await api(`/api/intake-batches/${batch.id}/recognize?limit=5`,
    { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' })).data
  const card = await waitUntil(async () => {
    const row = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .find((item) => item.id === datedItem.id)
    return row && row.recognition.status === 'done' ? row : null
  }, 'the dated page is read')
  findings.us_date = {
    raw: card.recognition.date_raw_text,
    assumed_order: card.recognition.date_assumed_order,
    readings: card.recognition.date_readings.map((entry) => entry.value),
    chips: card.candidates.filter((entry) => entry.field === 'purchase_date')
      .map((entry) => entry.value),
    decided: card.decided_purchase_date,
    model_calls: recognized.model_calls,
  }
  assert(findings.us_date.raw === '6/3/2026'
    && findings.us_date.chips.length === 1
    && findings.us_date.chips[0] === '2026-06-03'
    && findings.us_date.assumed_order === 'mdy'
    && findings.us_date.decided === null,
    `6/3/2026 该读成 6 月 3 日、只给一条候选：${JSON.stringify(findings.us_date)}`)
  // 「直接分类」：它自己出现在 6 月 3 日那一组里，而且标着「系统建议」。
  const groups = (await api(`/api/intake-batches/${batch.id}/groups?suggested=true`)).data.groups
  findings.us_date_group = groups.filter((group) => group.purchase_date === '2026-06-03')
    .map((group) => ({ source: group.source_name, items: group.item_count }))
  assert(findings.us_date_group.some((group) => group.items >= 1),
    `这一页该直接落在 2026-06-03 组里：${JSON.stringify(groups.map((g) => g.purchase_date))}`)
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('6 月 3 日'), 'the June 3 group on the board')
  findings.us_date_note = await cdp.eval(`(() => {
    const card = [...document.querySelectorAll('.card')]
      .find((node) => node.innerText.includes('dated-invoice.pdf'))
    return card ? card.innerText.replace(/\s+/g, ' ').trim() : '' })()`)
  assert(!findings.us_date_note.includes('有两种读法'),
    `卡片不该再问「这是几月」：${findings.us_date_note}`)

  // --- 第 17 节 ①②③④⑤ -------------------------------------------------
  //
  // 夹具就用刚才那张 `dated-invoice.pdf`：纸面读出的商家是 Hudson Street
  // Provisions，和这一批里另一家 Smoke Market 不是一回事——正是用户踩到的那种纸。
  const datedBefore = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
    .find((row) => row.id === datedItem.id)
  // 先把它放到另一天（Day2），这样下一步才是「从 Day2 拖进 Day1」。
  await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'smoke-s17-day2', action: 'date',
    items: [{ id: datedBefore.id, version: datedBefore.version }],
    purchase_date: '2026-06-18' }))
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('Hudson Street Provisions'),
    'the dated card sits under its own supplier')

  // ② 整块日期区都是「只定这一天」的投放区；商家组各有自己的 key（嵌在里面）。
  findings.drop_zones = await cdp.eval(`(() => {
    const section = [...document.querySelectorAll('.ui-card.section')]
      .find((node) => node.innerText.includes('6 月 3 日'))
    return { section: section.getAttribute('data-drop-key'),
      groups: [...section.querySelectorAll('.group')].map((node) => node.getAttribute('data-drop-key')) }
  })()`)
  assert(findings.drop_zones.section === '["date","2026-06-03"]',
    `整个 6 月 3 日区块该是「只定这一天」：${JSON.stringify(findings.drop_zones)}`)
  assert(findings.drop_zones.groups.every((key) => key && key.startsWith('["group","2026-06-03"')),
    `商家组仍然各有自己的投放目标：${JSON.stringify(findings.drop_zones)}`)

  // ② 把它移进别人家的组：先问一句，不静默合并。
  await cdp.eval(`([...document.querySelectorAll('[aria-label*="选中"]')]
    .find((node) => node.getAttribute('aria-label').includes('dated-invoice.pdf'))).click()`)
  await waitUntil(async () => (await cdp.text()).includes('已选 1 项'), 'the dated card is picked')
  await cdp.eval(`(() => { const group = [...document.querySelectorAll('.group')]
      .find((node) => node.innerText.includes('Smoke Market')
        && node.closest('.ui-card.section').innerText.includes('6 月 3 日'))
    const button = [...group.querySelectorAll('button,uni-button')]
      .find((node) => node.innerText.includes('移入所选'))
    button.click(); return true })()`)
  findings.source_clash = await waitUntil(async () => {
    const text = await cdp.eval(`document.querySelector('.overlay')?.innerText ?? ''`)
    return text.includes('已经有商家') ? text.replace(/\s+/g, ' ').trim() : null
  }, 'the clash dialog')
  assert(findings.source_clash.includes('Hudson Street Provisions')
    && findings.source_clash.includes('Smoke Market'),
    `对话框要说出两边是谁：${findings.source_clash}`)
  await cdp.click('只改日期，保留原商家', `document.querySelector('.overlay')`)
  const keptSource = await waitUntil(async () => {
    const row = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .find((item) => item.id === datedItem.id)
    return row && row.decided_purchase_date === '2026-06-03' ? row : null
  }, 'moved to 6 月 3 日 without taking the other supplier')
  assert(keptSource.decided_source_name === null, `商家不该被改掉：${JSON.stringify(keptSource)}`)
  const dayOneGroups = (await api(`/api/intake-batches/${batch.id}/groups?suggested=true`)).data.groups
    .filter((group) => group.purchase_date === '2026-06-03')
    .map((group) => ({ source: group.source_name, items: group.item_count }))
  findings.day_one_groups = dayOneGroups
  assert(dayOneGroups.some((group) => group.source === 'Hudson Street Provisions' && group.items === 1),
    `6 月 3 日下面该多出一个自己的商家组：${JSON.stringify(dayOneGroups)}`)

  // ① 卡片上的「改商家」：只改这一张，日期不动。
  // ⚠️ 等**这张卡片**的那个按钮，而不是等页面上出现「改商家」三个字：组内卡片是
  // 移动之后重新读进来的，别的卡片早就带着这三个字了。
  const editSourceButton = `([...document.querySelectorAll('[aria-label^="修改"]')]
    .find((node) => node.getAttribute('aria-label').includes('dated-invoice.pdf')))`
  await waitUntil(() => cdp.eval(`!!${editSourceButton}`), 'the per-card supplier button')
  await cdp.eval(`${editSourceButton}.click()`)
  findings.card_source_sheet = await waitUntil(async () => {
    const text = await cdp.eval(`document.querySelector('.overlay')?.innerText ?? ''`)
    return text.includes('的商家') ? text.replace(/\s+/g, ' ').trim() : null
  }, 'the one-card supplier sheet')
  assert(findings.card_source_sheet.includes('dated-invoice.pdf')
    && findings.card_source_sheet.includes('采购日期不动'),
    `这张面板该说清楚它只改一张：${findings.card_source_sheet}`)
  await cdp.click('Smoke Market', `document.querySelector('.overlay')`)
  await cdp.click('保存', `document.querySelector('.overlay')`)
  const renamed = await waitUntil(async () => {
    const row = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .find((item) => item.id === datedItem.id)
    return row && row.decided_source_name === 'Smoke Market' ? row : null
  }, 'the single card changed supplier')
  assert(renamed.decided_purchase_date === '2026-06-03',
    `改商家不该动日期：${JSON.stringify(renamed)}`)
  findings.card_source_changed = { source: renamed.decided_source_name,
    date: renamed.decided_purchase_date }

  // ③⑤④ 填表页：没有那张「识别进度」，来源有绿/红按钮，右上角是「删除本张单据」。
  await cdp.eval(`([...document.querySelectorAll('[aria-label^="开始填写"]')]
    .find((node) => node.getAttribute('aria-label').includes('dated-invoice.pdf'))).click()`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'), 'fill page')
  const deletableId = Number(await cdp.eval(`new URLSearchParams(location.search).get('invoice')`))
  await waitUntil(async () => (await cdp.text()).includes('单据信息'), 'the header card')
  const fillPage = await cdp.text()
  findings.no_extraction_card = { has_card: fillPage.includes('识别进度'),
    has_sentence: fillPage.includes('请先上传可识别的单据文件') }
  assert(!findings.no_extraction_card.has_card && !findings.no_extraction_card.has_sentence,
    `批次来的单据不该再顶着一张「识别进度」：${JSON.stringify(findings.no_extraction_card)}`)

  findings.source_review = await cdp.eval(`(() => {
    const field = [...document.querySelectorAll('.header-card .field')]
      .find((node) => node.innerText.startsWith('来源'))
    return { text: field.innerText.replace(/\\s+/g, ' ').trim(),
      buttons: [...field.querySelectorAll('button,uni-button')].map((node) => node.innerText.trim()) }
  })()`)
  assert(findings.source_review.buttons.includes('此来源正确')
    && findings.source_review.buttons.includes('修改'),
    `来源要有绿/红那一对：${JSON.stringify(findings.source_review)}`)
  assert(!findings.source_review.text.includes('以下是识别线索')
    && !findings.source_review.text.includes('纸面英文商家名'),
    `那堆证据该没了：${JSON.stringify(findings.source_review)}`)
  assert(findings.source_review.text.includes('Hudson Street Provisions'),
    `纸面读的和填着的不一样时，那一个值要说出来：${JSON.stringify(findings.source_review)}`)
  await cdp.eval(`(() => { const field = [...document.querySelectorAll('.header-card .field')]
    .find((node) => node.innerText.startsWith('来源'))
    ;[...field.querySelectorAll('button,uni-button')]
      .find((node) => node.innerText.trim() === '此来源正确').click() })()`)
  await waitUntil(async () => !(await cdp.eval(`document.querySelector('.header-card .field')?.innerText.includes('此来源正确')`)),
    'source check hidden after confirmation')
  const otherConfirm = await cdp.eval(`(() => {
    const fields = [...document.querySelectorAll('.header-card .field')]
    const button = fields.flatMap((field) => [...field.querySelectorAll('button,uni-button')])
      .find((node) => node.innerText.trim() === '此数据正确')
    if (button) button.click()
    return Boolean(button) })()`)
  assert(otherConfirm, '合成单据需要另一个待确认字段以复现来源按钮重现问题')
  await waitUntil(async () => (await api(`/api/invoices/${deletableId}`)).data.header_reviewed_fields.includes('invoice_number'),
    'another header field is stored')
  findings.source_check_stays_hidden = !(await cdp.eval(`document.querySelector('.header-card .field')?.innerText.includes('此来源正确')`))
  assert(findings.source_check_stays_hidden, '确认其它字段后不能再次要求确认来源')

  findings.delete_entry = await cdp.eval(`(() => {
    const bar = document.querySelector('.detail-heading')
    return { buttons: [...bar.querySelectorAll('button,uni-button')].map((node) => node.innerText.trim()) }
  })()`)
  assert(findings.delete_entry.buttons.includes('删除本张单据')
    && !findings.delete_entry.buttons.includes('单据更多操作'),
    `右上角该是「删除本张单据」：${JSON.stringify(findings.delete_entry)}`)
  await cdp.click('删除本张单据')
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.uni-modal')`), 'delete prompt')
  findings.delete_prompt = (await cdp.eval(`document.querySelector('.uni-modal').innerText`))
    .replace(/\s+/g, ' ').trim()
  assert(findings.delete_prompt.includes('是否清空数据，并彻底移除本张单据'),
    `问的那句话要照用户说的写：${findings.delete_prompt}`)
  await cdp.eval(`document.querySelector('.uni-modal__btn_primary').click()`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=organize'),
    `删完该回到整理页，得到 ${await cdp.eval('location.search')}`)
  // ⚠️ 单据行本身是**作废**而不是从库里抹掉（它可能背着已作废的采购记录，那是
  // 价格历史的凭据）：用户看得见的地方——单据库列表和整理页——都不再有它。
  const deletedRow = (await api(`/api/invoices/${deletableId}`, { allowError: true })).data
  assert(deletedRow.status === 'voided', `那张单据该是已作废：${JSON.stringify(deletedRow.status)}`)
  assert(!(await api('/api/invoices')).data.items.some((row) => row.id === deletableId),
    '单据库的默认列表里不该再有它')
  assert((await api(`/api/invoices/${deletableId}`)).data.intake_sources.length === 0,
    '它和整理页的那几页内容已经断开')
  const leftOnBoard = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
  assert(!leftOnBoard.some((row) => row.id === datedItem.id),
    '删掉的单据不该在整理页上留下卡片')
  await waitUntil(async () => !(await cdp.text()).includes('dated-invoice.pdf'),
    'the card is off the board')
  findings.deleted_invoice = { invoice: 404, item_on_board: false,
    items_left: leftOnBoard.length }

  /**
   * 在**当前这张**填表页上走完一行：加一行 → 填 → 保存 → 选分类 → 入账 →
   * 勾「我已检查原件有无漏行」→ 完成单据。第 20 节那一轮的逐项断言不在这里重复，
   * 这个只是为了把一张单据真的填完。
   */
  async function fillOneLineAndComplete() {
    const invoiceId = Number(await cdp.eval(`new URLSearchParams(location.search).get('invoice')`))
    await cdp.click('加一行')
    await waitUntil(() => cdp.eval(`!!document.querySelector('[aria-label="商品名称"]')`), 'line editor')
    await cdp.eval(`(() => { const set = (label, value) => {
        const found = document.querySelector('[aria-label="' + label + '"]');
        const node = found.tagName === 'INPUT' ? found : found.querySelector('input');
        node.value = value;
        node.dispatchEvent(new Event('input', { bubbles: true }));
        node.dispatchEvent(new Event('change', { bubbles: true })); };
      set('商品名称', 'Smoke Rice'); set('数量', '1'); set('采购单位', 'lb');
      set('单价', '3.00'); set('金额', '3.00'); return true })()`)
    await waitUntil(async () => (await cdp.text()).includes('保存这一行'), 'line is dirty')
    await cdp.click('保存这一行')
    await waitUntil(async () => (await cdp.text()).includes('没有未保存的修改'), 'line saved')
    // 已经建过的食材会被自动匹配上，那时没有分类可选——两种情形都要走得通。
    if (await cdp.eval(`!!document.querySelector('.category-field .select-button')`)) {
      await cdp.eval(`document.querySelector('.category-field .select-button').click()`)
      await waitUntil(() => cdp.eval(`!!document.querySelector('.category-menu .option')`), 'category menu')
      await cdp.eval(`document.querySelector('.category-menu .option').click()`)
    }
    await cdp.click('确认此行并入账')
    await waitUntil(async () => (await api(`/api/invoices/${invoiceId}`)).data
      .reconciliation.pending_count === 0, 'line booked')
    await cdp.click('检查原件并完成单据')
    await waitUntil(async () => (await cdp.text()).includes('我已检查原件有无漏行'), 'closeout form')
    await cdp.eval(`(() => { const row = document.querySelector('[aria-label="完成单据"] .check-line')
      const words = [...row.querySelectorAll('*')]
        .find((node) => node.textContent.trim() === '我已检查原件有无漏行') ?? row
      words.click(); return true })()`)
    await waitUntil(() => cdp.eval(`document.querySelector('[aria-label="完成单据"] .check-line')
      .getAttribute('aria-checked') === 'true'`), 'the box is ticked')
    await cdp.eval(`(() => { const target = [...document.querySelectorAll('button,uni-button')]
      .find((node) => node.textContent.trim() === '完成单据')
      if (!target) throw new Error('no 完成单据 button'); target.click(); return true })()`)
  }

  // --- 第 22 节 ①：填完一张回整理页，刷新之后也一样 ------------------------
  //
  // ⚠️ 这里**故意整页重新加载**再进填表页：用户踩到的就是这条路——「从哪进来的」
  // 原来只是内存里的一个 ref，刷新一下就回到默认的「单据库」，于是填完一张被丢回
  // 单据库，而第 11 节 ⑤ 说好的是整批在整理页里走完。
  const openDraft = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
    .find((row) => row.review_status === 'draft' && row.review_invoice_id)
  assert(openDraft, '这一步需要一张还在填的草稿')
  await cdp.go(`http://127.0.0.1:${WEB}/?view=review&invoice=${openDraft.review_invoice_id}`)
  await waitUntil(async () => (await cdp.text()).includes('加一行'), 'the reloaded draft')
  await fillOneLineAndComplete()
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=organize'),
    `刷新之后填完也该回整理页，得到 ${await cdp.eval('location.search')}`)
  findings.completed_after_reload = { invoice: openDraft.review_invoice_id, landed: 'view=organize' }

  // --- 第 22 节 ②：确认完成所有填写 ---------------------------------------
  const readFinish = () => cdp.eval(`(() => {
    const button = [...document.querySelectorAll('button,uni-button')]
      .find((node) => node.textContent.trim() === '确认完成所有填写')
    if (!button) return null
    const card = button.closest('.ui-card')
    return { disabled: button.hasAttribute('disabled'),
      title: button.getAttribute('title'),
      note: card.innerText.replace(/\s+/g, ' ').trim(),
      background: getComputedStyle(button).backgroundColor,
      last_on_the_board: card === card.parentElement.lastElementChild } })()`)
  findings.finish_blocked = await waitUntil(readFinish, '整理页底下要有「确认完成所有填写」')
  assert(findings.finish_blocked.disabled === true
    && /还有 \d+ 项没有填完/.test(findings.finish_blocked.note),
    `还没填完时它该暗着并说出还差什么：${JSON.stringify(findings.finish_blocked)}`)
  assert(findings.finish_blocked.last_on_the_board,
    `它该在最后一组的下面：${JSON.stringify(findings.finish_blocked)}`)

  // 剩下那几项走接口填完（本节要验的是那个按钮，不是再走一遍填表界面）。
  const foodItemId = (await api('/api/food-items')).data.items[0].id
  for (let round = 0; round < 10; round += 1) {
    const left = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
      .filter((row) => row.review_status !== 'completed')
    if (!left.length) break
    let row = left[0]
    if (!row.decided_purchase_date || !row.decided_source_name) {
      await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
        request_key: `smoke-s22-${row.id}`, action: 'group',
        items: [{ id: row.id, version: row.version }],
        purchase_date: '2026-06-03', source_name: 'Smoke Market' }))
      row = (await api(`/api/intake-batches/${batch.id}/items`)).data.items
        .find((item) => item.id === row.id)
    }
    let invoiceId = row.review_invoice_id
    if (!invoiceId) {
      invoiceId = (await api(`/api/intake-batches/${batch.id}/reviews`, json('POST', {
        items: [{ id: row.id, version: row.version }] }))).data.invoice_id
    }
    const invoice = (await api(`/api/invoices/${invoiceId}`)).data
    if (!invoice.lines.length) {
      await api(`/api/invoices/${invoiceId}/lines`, json('POST', {
        description: 'Smoke Rice', quantity: '1', purchase_unit: 'lb',
        unit_price: '3.00', line_amount: '3.00' }))
    }
    const pending = (await api(`/api/invoices/${invoiceId}`)).data.lines
      .filter((line) => line.status === 'pending')
    if (pending.length) {
      await api(`/api/invoices/${invoiceId}/confirm`, json('POST', {
        lines: pending.map((line) => ({ line_id: line.id, food_item_id: foodItemId })) }))
    }
    await api(`/api/invoices/${invoiceId}/complete`, json('POST', {}))
  }
  const finishedBatch = (await api(`/api/intake-batches/${batch.id}`)).data
  assert(finishedBatch.unfinished_item_count === 0,
    `这一批该一项不剩：${JSON.stringify(finishedBatch.unfinished_item_count)}`)

  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  findings.finish_ready = await waitUntil(async () => {
    const state = await readFinish()
    return state && !state.disabled ? state : null
  }, '全部填完之后那个按钮该按得动')
  assert(findings.finish_ready.background === 'rgb(23, 107, 77)',
    `它是绿的：${JSON.stringify(findings.finish_ready)}`)
  assert(findings.finish_ready.note.includes('全部填完了'),
    `按得动的时候也该说一句它会做什么：${JSON.stringify(findings.finish_ready)}`)
  // 收口前检查窄屏整理页；收口后不应再进入这张分类页面。
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 2, mobile: true })
  await cdp.go(`http://127.0.0.1:${WEB}/`)
  await waitUntil(async () => (await cdp.text()).includes('上传单据'), 'narrow home')
  const narrowHome = await cdp.eval('document.documentElement.scrollWidth - document.documentElement.clientWidth')
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('待归类'), 'narrow open board')
  await waitUntil(() => cdp.eval(`!!document.querySelector('.new-date .date-field')`), 'narrow date field')
  assert(await cdp.eval(`(() => {
    const tray = document.querySelector('.floating-stickers')
    return !!tray && getComputedStyle(tray).position === 'fixed'
      && !!document.querySelector('.new-date')
  })()`), '移动端贴片固定在视口内，日期分组同时可见')
  const stickerTop = await cdp.eval(`document.querySelector('.floating-stickers').getBoundingClientRect().top`)
  await cdp.eval(`window.scrollTo(0, 700)`)
  await sleep(150)
  assert(Math.abs((await cdp.eval(`document.querySelector('.floating-stickers').getBoundingClientRect().top`)) - stickerTop) < 2,
    '移动端滚动时贴片留在视口内')
  await cdp.eval(`([...document.querySelectorAll('.floating-stickers .column-head button,.floating-stickers .column-head uni-button')].at(-1)).click()`)
  assert(await cdp.eval(`!document.querySelector('.floating-stickers') && !!document.querySelector('.collapsed-sticker')`),
    '贴片收起后变成普通页面中的待归类入口')
  await cdp.eval(`document.querySelector('.collapsed-sticker').click()`)
  assert(await cdp.eval(`!!document.querySelector('.floating-stickers')`), '点击收起入口后重新展开贴片')
  const stickerBeforeDrag = await cdp.eval(`document.querySelector('.floating-stickers').getBoundingClientRect().left`)
  await cdp.eval(`(() => {
    const grip = document.querySelector('.sticker-grip')
    const point = grip.getBoundingClientRect()
    grip.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 91,
      pointerType: 'touch', isPrimary: true, clientX: point.left + 20, clientY: point.top + 20 }))
  })()`)
  await sleep(380)
  await cdp.eval(`(() => {
    const point = document.querySelector('.sticker-grip').getBoundingClientRect()
    window.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 91,
      pointerType: 'touch', isPrimary: true, clientX: point.left + 56, clientY: point.top + 20 }))
    window.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 91,
      pointerType: 'touch', isPrimary: true, clientX: point.left + 56, clientY: point.top + 20 }))
  })()`)
  assert((await cdp.eval(`document.querySelector('.floating-stickers').getBoundingClientRect().left`)) > stickerBeforeDrag + 20,
    '长按贴片顶部后可改变屏幕位置')
  const narrowBoard = await cdp.eval('document.documentElement.scrollWidth - document.documentElement.clientWidth')
  assert(narrowHome <= 0 && narrowBoard <= 0,
    `horizontal overflow at 390px: home ${narrowHome}, board ${narrowBoard}`)
  findings.narrow = { width: 390, home_overflow: narrowHome, board_overflow: narrowBoard,
    date_boxes: await cdp.eval(`document.querySelectorAll('.new-date .date-field input').length`) }
  await cdp.send('Emulation.clearDeviceMetricsOverride')
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.text()).includes('确认完成所有填写'), 'desktop open board')
  await cdp.click('确认完成所有填写')
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=invoices'),
    `点完该落到单据库，得到 ${await cdp.eval('location.search')}`)
  // 落在「已完成」那一页：刚填完的那几张就在那里。
  findings.landed_tab = await waitUntil(() => cdp.eval(`(() => {
    const tab = [...document.querySelectorAll('.tabs button,.tabs uni-button')]
      .find((node) => node.getAttribute('aria-current') === 'page')
    return tab ? tab.textContent.trim() : null })()`), '单据库的页签')
  assert(findings.landed_tab === '已完成',
    `该落在「已完成」那一页：${findings.landed_tab}`)
  const noCurrent = await api('/api/intake-batches/current', { allowError: true })
  assert(noCurrent.status === 404,
    `收口之后 /current 该是 404：${noCurrent.status}`)
  assert((await api(`/api/intake-batches/${batch.id}`)).data.status === 'closed',
    '这一批在服务端是已结束')
  const completedSource = (await api(`/api/intake-batches/${batch.id}/items?limit=200`)).data.items
    .find((item) => item.review_invoice_id)
  assert(completedSource, '收口批次应有可从单据库打开的已完成单据')
  await waitUntil(() => cdp.eval(`!!document.querySelector('[data-invoice-id="${completedSource.review_invoice_id}"] .icon-open')`),
    'completed invoice icon after close')
  await cdp.eval(`document.querySelector('[data-invoice-id="${completedSource.review_invoice_id}"] .icon-open').click()`)
  const closedReceiptBack = await waitUntil(() => cdp.eval(`document.querySelector('.receipt-page .back-link')?.textContent.trim()`),
    'completed invoice receipt back link')
  assert(closedReceiptBack === '← 返回单据',
    `收口后已完成单据只能返回单据库：${closedReceiptBack}`)
  await cdp.click('返回单据')
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=invoices'),
    'completed invoice returns to library')
  // 首页那个「接着整理」靠的就是 /current，所以它必须跟着消失。
  await cdp.go(`http://127.0.0.1:${WEB}/`)
  await waitUntil(async () => (await cdp.text()).includes('上传单据'), 'home again')
  findings.resume_gone = { home_has_resume: (await cdp.text()).includes('接着整理'),
    batch_status: (await api(`/api/intake-batches/${batch.id}`)).data.status }
  assert(!findings.resume_gone.home_has_resume,
    `首页不该再有「接着整理」：${JSON.stringify(findings.resume_gone)}`)

  // 已收口的历史链接也应回单据库，不能重新打开分类页面。
  await cdp.go(`http://127.0.0.1:${WEB}/?view=organize&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=invoices'),
    'closed board URL redirects to invoice library')
  assert(!(await cdp.text()).includes('返回分类页面'), '已结束的分类页面不再渲染')
  await cdp.go(`http://127.0.0.1:${WEB}/?view=ledger&batch=${batch.id}`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=invoices'),
    'closed ledger URL redirects to invoice library')

  console.log(JSON.stringify({ passed: true, ...findings }, null, 2))
} finally {
  cdp?.socket.close()
  if (server) await new Promise((resolve) => server.close(resolve))
  for (const child of children.reverse()) {
    if (child.pid && child.exitCode === null) {
      spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true })
    }
  }
  const tempRoot = fs.realpathSync(os.tmpdir())
  const target = fs.realpathSync(work)
  if (target.startsWith(`${tempRoot}${path.sep}`) && path.basename(target).startsWith('inventory-browser-')) {
    fs.rmSync(target, { recursive: true, force: true })
  } else {
    throw new Error(`Refusing to remove unexpected smoke directory: ${target}`)
  }
}
