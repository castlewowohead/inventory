/**
 * Isolated H5 smoke for the 2026-11-23 invoice ledger rework. Run from repo root.
 *
 * 验的是 docs/multifiles.md 第 11 节那三条：
 *   ① 一张发票一条记录，单号是唯一的拆分依据，读不出单号就一页一张；
 *   ② 两个页签回答两个问题：「待处理」是待办清单（卡片、不筛、按录入时间倒序），
 *      「已完成」是翻历史（可切换按收据日期/创建时间排序，可选定时间范围）；
 *   ③ 批次来的单据显示真实原件出处（哪个文件第几页），手工上传的不写张数（第 13 节）。
 *
 * 用 Fake 识别器与合成 PDF，**不调用真实 OCR**，所以它不能当识别准确率的证据。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import net from 'node:net'
import { spawn, spawnSync } from 'node:child_process'

const root = process.cwd()
const visual = process.argv.includes('--visual')
const backend = path.join(root, 'backend')
const frontend = path.join(root, 'frontend', 'dist', 'build', 'h5')
const python = path.join(backend, '.venv', 'Scripts', 'python.exe')
const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'inventory-browser-'))
const db = path.join(work, 'smoke.db').replaceAll('\\', '/')
const env = { ...process.env, APP_ENV: 'testing', PORT: '5321', HOST: '127.0.0.1',
  OCR_PROVIDER: 'fake', DATABASE_URL: `sqlite:///${db}`,
  FILE_STORAGE_DIR: path.join(work, 'uploads'), SECRET_KEY: 'isolated-ledger-smoke' }
const children = []
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
async function waitUntil(check, label, max = 20000) {
  const start = Date.now()
  while (Date.now() - start < max) {
    try { const result = await check(); if (result) return result } catch {}
    await sleep(150)
  }
  throw new Error(`Timed out: ${label}`)
}
function start(command, args, cwd = root, extraEnv = env) {
  const child = spawn(command, args, { cwd, env: extraEnv, windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'] })
  children.push(child)
  child.logs = ''
  child.stdout.on('data', (data) => { child.logs = (child.logs + String(data)).slice(-8000) })
  child.stderr.on('data', (data) => { child.logs = (child.logs + String(data)).slice(-8000) })
  return child
}
function staticServer() {
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
    '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
    '.woff2': 'font/woff2', '.wasm': 'application/wasm' }
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    if (pathname.startsWith('/api/') || pathname === '/health') {
      const proxy = http.request({ hostname: '127.0.0.1', port: 5321,
        method: request.method, path: request.url, headers: request.headers }, (incoming) => {
        response.writeHead(incoming.statusCode, incoming.headers)
        incoming.pipe(response)
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
  server.listen(5322, '127.0.0.1')
  return server
}
async function api(url, options = {}) {
  const response = await fetch(`http://127.0.0.1:5321${url}`, options)
  const data = await response.json()
  assert(response.ok, `${url}: ${response.status} ${JSON.stringify(data)} ${children[0]?.logs ?? ''}`)
  return data
}
function json(method, data) {
  return { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(data) }
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
    const result = await this.send('Runtime.evaluate', { expression,
      returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) {
      throw new Error(`${expression}: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`)
    }
    return result.result.value
  }
}

/** 点一个按钮：uni-app 在 H5 下渲染成 uni-button，不是原生 button。 */
function clickByText(text, scope = 'button,uni-button') {
  return `([...document.querySelectorAll(${JSON.stringify(scope)})]
    .find((node) => node.textContent.trim() === ${JSON.stringify(text)})).click()`
}

let server
let cdp
try {
  for (const port of [5321, 5322, 9334]) await requireFreePort(port)
  runPython(['-m', 'alembic', 'upgrade', 'head'])
  start(python, ['app.py'], backend)
  await waitUntil(async () => (await fetch('http://127.0.0.1:5321/health')).ok, 'Flask')
  server = staticServer()
  await waitUntil(async () => (await fetch('http://127.0.0.1:5322/')).ok, 'H5 server')

  // 一份四页 PDF：第 1、2 页同一个纸面单号（一张两页发票），第 3 页读不出单号，
  // 第 4 页是另一天另一家。
  const batch = await api('/api/intake-batches', { method: 'POST' })
  const pdf = path.join(work, 'four.pdf')
  runPython(['-c', 'from pypdf import PdfWriter; import sys; w=PdfWriter(); [w.add_blank_page(width=612,height=792) for _ in range(4)]; w.write(sys.argv[1])', pdf])
  const form = new FormData()
  form.append('file', new Blob([fs.readFileSync(pdf)], { type: 'application/pdf' }), 'four.pdf')
  await api(`/api/intake-batches/${batch.id}/files`, { method: 'POST', body: form })
  const items = await waitUntil(async () => {
    const result = await api(`/api/intake-batches/${batch.id}/items`)
    return result.total === 4 ? result.items : null
  }, 'four PDF pages')

  const numbers = ['A-1', 'A-1', null, 'B-9']
  const totals = ['120.00', null, '88.50', '310.25']
  runPython(['-c', `import sqlite3,json,sys
c=sqlite3.connect(sys.argv[1])
for ident,number,total in json.loads(sys.argv[2]):
    fields={}
    if number: fields['invoice_number']={'status':'recognized','raw_text':number}
    else: fields['invoice_number']={'status':'unreadable','raw_text':None}
    if total: fields['invoice_total']={'status':'recognized','raw_text':total}
    c.execute('UPDATE intake_items SET recognition_payload=? WHERE id=?',(json.dumps({'fields':fields}),ident))
c.commit()`, db, JSON.stringify(items.map((item, index) => [item.id, numbers[index], totals[index]]))])

  const first = await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'ledger-june', action: 'group',
    items: items.slice(0, 3).map(({ id, version }) => ({ id, version })),
    purchase_date: '2026-06-06', source_name: 'Smoke Market' }))
  assert(first.items.length === 3, 'group three June pages')
  await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'ledger-july', action: 'group',
    items: [{ id: items[3].id, version: items[3].version + 0 }],
    purchase_date: '2026-07-02', source_name: 'Other Market' }))

  // ① 边界：同号两页合一张，无单号那页自己一张。
  const fresh = (await api(`/api/intake-batches/${batch.id}/items`)).items
  const june = await api(`/api/intake-batches/${batch.id}/reviews/selection`, json('POST', {
    items: fresh.slice(0, 3).map(({ id, version }) => ({ id, version })) }))
  assert(new Set(june.invoice_ids).size === 2,
    `同号合并 + 无单号单独成张：${JSON.stringify(june)}`)
  const july = await api(`/api/intake-batches/${batch.id}/reviews/selection`, json('POST', {
    items: [{ id: fresh[3].id, version: fresh[3].version }] }))
  assert(july.invoice_ids.length === 1, 'July page opens one draft')

  // 一张没有日期的单据，用来验「日期待填」筛选与排序。
  const undatedForm = new FormData()
  undatedForm.append('files', new Blob([Buffer.from('\x89PNG\r\n\x1a\n undated')], { type: 'image/png' }), 'undated.png')
  undatedForm.append('recognize', 'false')
  const undated = await api('/api/invoices', { method: 'POST', body: undatedForm })
  // 第 13 节的「按值写」要两边都验到：这一张留一行待核对，其余几张一行都没有。
  await api(`/api/invoices/${undated.id}/lines`, json('POST', { description: '待核对占位行' }))

  start(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-allow-origins=*',
    '--remote-debugging-port=9334', `--user-data-dir=${path.join(work, 'edge')}`,
    '--window-size=1280,900', 'http://127.0.0.1:5322/?view=invoices'])
  const target = await waitUntil(async () => {
    const pages = await (await fetch('http://127.0.0.1:9334/json/list')).json()
    return pages.find((page) => page.type === 'page' && page.url.includes('5322'))
  }, 'Edge CDP')
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  cdp = new Cdp(socket)
  await cdp.send('Runtime.enable')
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('WORKSPACE / 单据'), 'ledger page')
  await waitUntil(async () => cdp.eval(`document.querySelectorAll('.icon-card').length >= 4`), 'default icon view')
  assert(!(await cdp.eval(`!!document.querySelector('.view-switch, .invoice-card')`)),
    '单据库只保留图标卡片，不显示视图切换')
  assert(await cdp.eval(`(() => {
    const card = document.querySelector('[data-invoice-id="${july.invoice_ids[0]}"]')
    const date = card?.querySelector('.icon-date')
    const title = card?.querySelector('.icon-title')
    if (!date || !title) return false
    const style = getComputedStyle(date)
    return date.textContent.trim() === '2026-07-02'
      && parseFloat(style.fontSize) >= 18
      && Number(style.fontWeight) >= 700
      && style.color === getComputedStyle(title).color
  })()`), '单据发生日期在图标卡片右上角以大号深色粗体显示')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
  })
  assert(await cdp.eval(`[...document.querySelectorAll('.icon-card')].every((card) => {
    const date = card.querySelector('.icon-date')
    const bounds = card.getBoundingClientRect()
    const dateBounds = date.getBoundingClientRect()
    return parseFloat(getComputedStyle(date).fontSize) >= 15
      && dateBounds.left >= bounds.left
      && dateBounds.right <= bounds.right + 1
  })`), '390px 窄屏日期保持至少 15px 且不越出卡片')
  await cdp.send('Emulation.clearDeviceMetricsOverride')

  // ② 待处理页签：图标卡片、没有筛选条、按录入时间倒序。
  const pendingCards = await waitUntil(async () => {
    const cards = await cdp.eval(`[...document.querySelectorAll('.icon-card')]
      .map((card) => card.dataset.invoiceId + '|' + card.innerText.replace(/\\s+/g, ' ').trim())`)
    return cards.length >= 4 ? cards : null
  }, 'pending cards')
  assert(!(await cdp.eval(`!!document.querySelector('.filter-bar')`)),
    '待处理页签不该有排序/筛选条')
  // 卡片上不印库里的 id，测试靠 data-invoice-id 认卡片。
  const idOf = (text) => Number(text.split('|')[0])
  assert(pendingCards.map(idOf).join() === [undated.id, ...july.invoice_ids, ...june.invoice_ids]
    .sort((a, b) => b - a).join(),
    `待处理按录入时间倒序：${JSON.stringify(pendingCards.map(idOf))}`)

  // ③ 图标卡片显示日期、已填写的总金额和有待办时的计数。
  const allPending = pendingCards.join(' | ')
  assert(!allPending.includes('单号'), `图标卡片不展示单号：${allPending}`)
  assert(allPending.includes('总金额 $120.00'), `已填写总金额的卡片应显示金额：${allPending}`)
  assert(!allPending.includes('单据 #'), `卡片不再印库里的单据 id：${allPending}`)
  assert(allPending.includes('2026-07-02'), `卡片显示单据日期：${allPending}`)
  assert(allPending.includes('日期待填'), `没有日期的说「日期待填」：${allPending}`)
  assert(!allPending.includes('已入账'), `卡片不再写已入账行数：${allPending}`)
  assert(!allPending.includes('待核对 0'), `没有待办就不写待核对：${allPending}`)
  const undatedCard = pendingCards.find((text) => idOf(text) === undated.id)
  assert(undatedCard.includes('待核对 1'), `有待办的卡片仍要写出来：${undatedCard}`)
  assert(!undatedCard.includes('总金额'), `未填写总金额的卡片留空：${undatedCard}`)

  // ④ 已完成页签：两种排序必须给出不同的次序，否则其中一个是摆设。
  //    为此让「录入次序」与「采购日期次序」故意相反。
  const mayForm = new FormData()
  const sampleImage = visual ? fs.readFileSync(path.join(backend, 'samples', 'real', '1.png'))
    : Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6WQAAAAASUVORK5CYII=', 'base64')
  mayForm.append('files', new Blob([sampleImage], { type: 'image/png' }), 'may.png')
  mayForm.append('recognize', 'false')
  const may = await api('/api/invoices', { method: 'POST', body: mayForm })
  await api(`/api/invoices/${may.id}`, json('PATCH', {
    invoice_date: '2026-05-01', source_name: 'May Market' }))
  for (const id of [july.invoice_ids[0], may.id]) {
    const line = await api(`/api/invoices/${id}/lines`, json('POST', { description: '占位行' }))
    await api(`/api/invoices/${id}/lines/${line.id}`, json('PATCH', { status: 'skipped' }))
    await api(`/api/invoices/${id}/complete`, { method: 'POST' })
  }
  // 录入次序：may 最后建，july 比它早；采购日期次序：july(07-02) 在 may(05-01) 前。
  await cdp.send('Page.navigate', { url: 'http://127.0.0.1:5322/?view=invoices' })
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('WORKSPACE / 单据'), 'ledger again')
  await waitUntil(async () => cdp.eval(`document.querySelectorAll('.icon-card').length > 0`), 'default icon view again')
  await cdp.eval(clickByText('已完成'))
  const readCards = async () => cdp.eval(`[...document.querySelectorAll('.icon-card')]
    .map((card) => card.dataset.invoiceId + '|' + card.innerText.replace(/\\s+/g, ' ').trim())`)
  const byDate = await waitUntil(async () => {
    const cards = await readCards()
    return cards.length === 2 ? cards.map(idOf) : null
  }, 'completed tab')
  assert((await cdp.eval(`document.querySelector('.sort-toggle').innerText`)).includes('按收据日期排序'),
    '已完成默认按收据日期排序')
  // 跳过行留在单据详情中，图标卡片不显示这项次要信息。
  const completedText = (await readCards()).join(' | ')
  assert(!completedText.includes('已跳过'), `图标卡片不显示已跳过行数：${completedText}`)
  assert(!completedText.includes('待核对'), `已完成卡片不写待核对：${completedText}`)
  assert(!completedText.includes('已入账'), `已完成卡片不写已入账：${completedText}`)
  assert(!completedText.includes('单据 #'), `已完成卡片也不印单据 id：${completedText}`)
  assert(completedText.includes('2026-07-02'), `已完成卡片显示单据日期：${completedText}`)
  assert(!completedText.includes('单号'), `已完成卡片也不展示单号：${completedText}`)
  assert(byDate.join() === [july.invoice_ids[0], may.id].join(),
    `按收据日期：07-02 在 05-01 前，得到 ${JSON.stringify(byDate)}`)
  const monthHeadings = await cdp.eval(`[...document.querySelectorAll('.month-heading')]
    .map((node) => node.innerText.replace(/\s+/g, ' ').trim())`)
  assert(monthHeadings.join('|') === '2026 年 7 月|2026 年 5 月',
    `已完成单据应按采购月份分段：${JSON.stringify(monthHeadings)}`)
  const monthIndex = await api('/api/invoices/months?completed=true&order=invoice_date')
  assert(monthIndex.months.map((month) => month.key).join() === '2026-07,2026-05'
    && monthIndex.months[1].offset === 1,
    `月份索引应覆盖全部页并给出跳转位置：${JSON.stringify(monthIndex)}`)
  await cdp.eval(`document.querySelector('[aria-label^="跳到 2026 年 5 月"]').click()`)
  await waitUntil(async () => cdp.eval(`document.querySelector('[data-month-key="2026-05"]')
    ?.getBoundingClientRect().top < innerHeight`), 'timeline scrolls to May')
  assert(await cdp.eval(`document.querySelector('[aria-label^="跳到 2026 年 5 月"]')
    ?.getAttribute('aria-current') === 'location'`), '时间轴选中五月时线条变长')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
  })
  assert(await cdp.eval(`document.documentElement.scrollWidth <= innerWidth + 1
    && document.querySelectorAll('.month-tick').length === 2`),
    '390px 窄屏月份时间轴应可见且没有横向溢出')
  await cdp.send('Emulation.clearDeviceMetricsOverride')

  // PDF 展示实际原件页的缩略图；坏 PNG 保留明确占位。
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.icon-pdf .document-image')`),
    'PDF original thumbnail')
  assert(await cdp.eval(`!document.querySelector('.icon-card .document-page')`),
    '图标卡片不显示原件页码')
  assert(await cdp.eval(`!!document.querySelector('.icon-photo')`), '图片有独立外轮廓')
  assert(await cdp.eval(`getComputedStyle(document.querySelector('.icon-photo')).borderTopColor === 'rgb(118, 80, 164)'`),
    '图片外框改为紫色，区别于 Word 常见的蓝色')
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.icon-photo .document-image')`),
    'image original thumbnail')
  assert(await cdp.eval(`(() => {
    const card = document.querySelector('.icon-pdf')
    const inner = card.querySelector('.document')
    return getComputedStyle(card).borderTopWidth === '2px'
      && getComputedStyle(inner).borderTopWidth === '0px'
  })()`), '文件类型外轮廓应在整张卡片上，不再嵌套第二个边框')
  if (visual) {
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
    const output = path.join(backend, 'var', 'icon-preview-visual.png')
    fs.mkdirSync(path.dirname(output), { recursive: true })
    fs.writeFileSync(output, Buffer.from(shot.data, 'base64'))
    console.log(`VISUAL SCREENSHOT ${output}`)
  }
  await cdp.eval(`document.querySelector('.sort-toggle').click()`)
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.sort-menu')`), 'sort menu')
  await cdp.eval(clickByText('按创建时间排序', '.sort-item'))
  const byCreation = await waitUntil(async () => {
    const cards = await readCards()
    return cards.length === 2 && idOf(cards[0]) === may.id ? cards.map(idOf) : null
  }, 'sorted by creation time')
  assert(byCreation.join() === [may.id, july.invoice_ids[0]].join(),
    `按创建时间：后录入的 may 在前，得到 ${JSON.stringify(byCreation)}`)
  assert(byCreation.join() !== byDate.join(), '两种排序必须真的不同，否则测不出区别')

  // ⑤ 「选定时间范围」：一个按钮展开起止两格，按单据上的采购日期筛。
  await cdp.eval(clickByText('选定时间范围', '.chip'))
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.range-panel')`), 'range panel')
  await cdp.eval(`(() => {
    const boxes = [...document.querySelectorAll('.range-panel input')]
    const fire = (box, value) => { box.value = value; box.dispatchEvent(new Event('input', { bubbles: true })) }
    fire(boxes[0], '2026'); fire(boxes[1], '07'); fire(boxes[2], '01')
    return true })()`)
  const fromJuly = await waitUntil(async () => {
    const cards = await readCards()
    return cards.length === 1 ? cards.map(idOf) : null
  }, '2026-07-01 起只剩七月那张')
  assert(fromJuly[0] === july.invoice_ids[0], `开区间筛选：${JSON.stringify(fromJuly)}`)
  assert((await cdp.eval(`[...document.querySelectorAll('.chip')][0].innerText`)).includes('2026年7月1日 起'),
    '按钮上要显示当前范围')
  await cdp.eval(clickByText('清除', '.range-panel uni-button,.range-panel button'))
  await waitUntil(async () => (await readCards()).length === 2, 'cleared range')

  // 已完成页签上只有排序下拉和「选定时间范围」两个控件。
  const chips = await cdp.eval(`[...document.querySelectorAll('.chip')].map((c) => c.innerText.trim())`)
  assert(chips.length === 1, `已完成页签只该有一个 chip，得到 ${JSON.stringify(chips)}`)

  // 导出先进入勾选模式，只允许选择已完成的单据。
  await cdp.eval(clickByText('导入表格 / 导出明细'))
  await waitUntil(async () => cdp.eval(`[...document.querySelectorAll('button,uni-button')]
    .some((node) => node.textContent.trim() === '下载单据')`), 'download invoice entry')
  await cdp.eval(clickByText('下载单据'))
  await waitUntil(async () => cdp.eval(`document.querySelectorAll('.export-check').length === 2`), 'export selection')
  await cdp.eval(`document.querySelector('[data-invoice-id="${may.id}"] .export-check').click()`)
  assert(await cdp.eval(`document.querySelector('[data-invoice-id="${may.id}"] .export-check').getAttribute('aria-checked')`) === 'true',
    '勾选一张已完成单据')
  await cdp.eval(clickByText('待处理'))
  await waitUntil(async () => cdp.eval(`document.querySelectorAll('.icon-card').length > 0
    && [...document.querySelectorAll('.export-check')].every((node) => node.hasAttribute('disabled'))`), 'pending export disabled')
  assert((await cdp.eval(`document.querySelector('.export-bar').innerText`)).includes('已选 1 张'),
    '换页签后已勾选的单据不丢')
  await cdp.eval(clickByText('已完成'))
  await waitUntil(async () => cdp.eval(`!!document.querySelector('[data-invoice-id="${may.id}"] .export-check')`), 'completed export again')
  await cdp.eval(clickByText('下载所选（1）'))
  await waitUntil(async () => cdp.eval(`!document.querySelector('.export-bar')`), 'selected download completed')

  // ⑦ 填表页的拆分 / 合并：去掉自动合并之后，这是把两页并回一张的唯一入口。
  const twoPageId = june.invoice_ids[0]
  await cdp.send('Page.navigate', { url: `http://127.0.0.1:5322/?view=review&invoice=${twoPageId}` })
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('原件文件'), 'review page')
  await cdp.eval(clickByText('原件文件 2 个', '.fold-toggle'))
  await waitUntil(async () => (await cdp.eval(`!!document.querySelector('.boundary')`)), 'boundary block')

  // 选中第 2 页并拆出去。确认框里点「继续」。
  await cdp.eval(`[...document.querySelectorAll('.source-pick')]
    .find((node) => node.getAttribute('aria-label').includes('第 2 页')).click()`)
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('拆出选中的 1 页'), 'split label')
  await cdp.eval(`[...document.querySelectorAll('uni-button,button')]
    .find((node) => node.textContent.includes('拆出选中的 1 页')).click()`)
  // uni.showModal 在 H5 下渲染成 .uni-modal，确认键是 .uni-modal__btn_primary。
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.uni-modal__btn_primary')`), 'confirm modal')
  assert(await cdp.eval(`(() => { const dialog = document.querySelector('.uni-modal')
    const title = dialog?.querySelector('.uni-modal__title')
    const action = dialog?.querySelector('.uni-modal__btn_primary')
    return getComputedStyle(dialog).backgroundColor === 'rgb(255, 255, 255)'
      && getComputedStyle(title).color === 'rgb(23, 59, 46)'
      && getComputedStyle(action).color === 'rgb(23, 107, 77)'
      && parseFloat(getComputedStyle(action).borderRadius) >= 8 })()`),
    '确认提示框应使用工作台的白、深绿和绿色操作样式')
  await cdp.eval(`document.querySelector('.uni-modal__btn_primary').click()`)
  const afterSplit = await waitUntil(async () => {
    const page = await api(`/api/invoices?completed=false&limit=50&date_from=2026-06-06&date_to=2026-06-06`)
    return page.total === 3 ? page : null
  }, `split created a third June draft`)
  const splitOff = afterSplit.items.find((row) => row.id !== twoPageId
    && row.intake_sources.some((source) => source.page_no === 2))
  assert(splitOff, `拆出的草稿应只含第 2 页：${JSON.stringify(afterSplit.items.map((r) => [r.id, r.intake_sources.map((s) => s.page_no)]))}`)
  assert(splitOff.invoice_total === null, '拆分后总额必须清空，不能把一个总额除开')

  // 再并回去：整张并走之后源草稿作废，界面跳到目标草稿。
  await cdp.send('Page.navigate', { url: `http://127.0.0.1:5322/?view=review&invoice=${splitOff.id}` })
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('原件文件'), 'split draft page')
  await cdp.eval(clickByText('原件文件 1 个', '.fold-toggle'))
  await waitUntil(async () => (await cdp.eval(`!!document.querySelector('.picker-face')`)), 'merge target picker')
  const mergedBack = await api(`/api/intake-batches/${batch.id}/reviews/${splitOff.id}/reassign`, json('POST', {
    items: splitOff.intake_sources.map((source) => ({ id: source.item_id, version: source.version })),
    target_invoice_id: twoPageId }))
  assert(mergedBack.target_invoice_id === twoPageId, `合并回原草稿：${JSON.stringify(mergedBack)}`)
  const reunited = await api(`/api/invoices/${twoPageId}`)
  assert(reunited.intake_sources.length === 2, `两页回到一张：${JSON.stringify(reunited.intake_sources)}`)

  // CSV 的缩略图展示保留下来的原始单元格，而不是编辑后的采购字段。
  const csvForm = new FormData()
  csvForm.append('file', new Blob(['商品描述,数量,单位,单价,金额,采购日期,采购来源\n鸡胸肉,2,lb,6.35,12.70,2026-10-01,Costco\n'],
    { type: 'text/csv' }), 'sample.csv')
  csvForm.append('options', '{}')
  const imported = await api('/api/imports', { method: 'POST', body: csvForm })
  await cdp.send('Page.navigate', { url: 'http://127.0.0.1:5322/?view=invoices' })
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.icon-table')`), 'default icon view with CSV')
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.icon-table .sheet-cell')`), 'CSV source cells')
  assert((await cdp.eval(`document.querySelector('[data-invoice-id="${imported.id}"]').innerText`)).includes('鸡胸肉'),
    'CSV 图标显示原始单元格内容')

  await cdp.eval(clickByText('已完成'))
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.icon-card[data-invoice-id="${july.invoice_ids[0]}"]')`),
    'completed card for original preview')
  await cdp.eval(`document.querySelector('.icon-card[data-invoice-id="${july.invoice_ids[0]}"] .icon-open').click()`)
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.receipt-page .inline-original .viewer')`),
    'completed original visible by default')
  assert(await cdp.eval(`document.querySelector('.inline-original').getBoundingClientRect().height > 350`),
    '已完成单据默认展开原件')
  assert(await cdp.eval(`!document.querySelector('.receipt-page .metadata').innerText.includes('单号')`),
    '已完成详情不展示单号')
  await cdp.eval(`document.querySelector('.original-button').click()`)
  assert(await cdp.eval(`!document.querySelector('.inline-original')`), '原件可收起')
  await cdp.eval(`document.querySelector('.original-button').click()`)
  assert(await cdp.eval(`!!document.querySelector('.inline-original .viewer')`), '原件可再次展开')

  // A month beyond the first 20 records must still be present on the rail.
  // These are synthetic rows in this disposable SQLite only; no OCR or user data.
  runPython(['-c', `import sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.executemany("INSERT INTO invoices (entry_source,header_reviewed_fields,created_at,updated_at,completed_at,invoice_date,source_name) VALUES ('invoice','[]','2026-06-15 00:00:00','2026-06-15 00:00:00','2026-06-15 00:00:00','2026-06-15',?)", [(f'Synthetic June {i}',) for i in range(21)])
db.commit()
db.close()`, db])
  await cdp.send('Page.navigate', { url: 'http://127.0.0.1:5322/?view=invoices' })
  await waitUntil(async () => cdp.eval(`!!document.querySelector('.tabs')`), 'ledger for long month index')
  await cdp.eval(clickByText('已完成'))
  await waitUntil(async () => cdp.eval(`!!document.querySelector('[aria-label^="跳到 2026 年 5 月"]')`), 'May month across pages')
  const manyMonths = await api('/api/invoices/months?completed=true&order=invoice_date')
  assert(manyMonths.months.find((month) => month.key === '2026-05')?.offset === 22,
    `五月应位于首批 20 张之外：${JSON.stringify(manyMonths.months)}`)
  await cdp.eval(`document.querySelector('[aria-label^="跳到 2026 年 5 月"]').click()`)
  await waitUntil(async () => cdp.eval(`!!document.querySelector('[data-invoice-id="${may.id}"]')`), 'May invoice after cross-page jump')
  assert((await cdp.eval(`document.querySelector('.pagination')?.innerText`)).includes('23–23 / 23'),
    '跨页跳月后分页位置应指向该月的首张单据')

  console.log(JSON.stringify({ passed: true, batch_id: batch.id,
    june_invoice_ids: june.invoice_ids, july_invoice_ids: july.invoice_ids,
    undated_invoice_id: undated.id, completed_may_invoice_id: may.id,
    split_off_invoice_id: splitOff.id,
    pending_order: pendingCards.map(idOf),
    completed_by_date: byDate, completed_by_creation: byCreation,
    completed_card_text: completedText }))
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
  if (target.startsWith(`${tempRoot}${path.sep}`)
      && path.basename(target).startsWith('inventory-browser-')) {
    fs.rmSync(target, { recursive: true, force: true })
  } else {
    throw new Error(`Refusing to remove unexpected smoke directory: ${target}`)
  }
}
