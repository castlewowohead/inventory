/** Isolated H5 smoke for the 2026-11-23 organizer fixes. Run from repo root. */
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
const env = { ...process.env, APP_ENV: 'testing', PORT: '5311', HOST: '127.0.0.1',
  OCR_PROVIDER: 'fake', DATABASE_URL: `sqlite:///${db}`,
  FILE_STORAGE_DIR: path.join(work, 'uploads'), SECRET_KEY: 'isolated-browser-smoke' }
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
  child.stdout.on('data', (data) => { child.logs += String(data); child.logs = child.logs.slice(-8000) })
  child.stderr.on('data', (data) => { child.logs += String(data); child.logs = child.logs.slice(-8000) })
  return child
}
function staticServer() {
  const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
    '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
    '.woff2': 'font/woff2', '.wasm': 'application/wasm' }
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    if (pathname.startsWith('/api/') || pathname === '/health') {
      const proxy = http.request({ hostname: '127.0.0.1', port: 5311,
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
  server.listen(5312, '127.0.0.1')
  return server
}
async function api(url, options = {}) {
  const response = await fetch(`http://127.0.0.1:5311${url}`, options)
  const data = await response.json()
  assert(response.ok, `${url}: ${response.status} ${JSON.stringify(data)} ${children[0]?.logs ?? ''}`)
  return data
}
function json(method, data) { return { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(data) } }

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
    if (result.exceptionDetails) throw new Error(`${expression}: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`)
    return result.result.value
  }
}

let server
let cdp
try {
  for (const port of [5311, 5312, 9333]) await requireFreePort(port)
  runPython(['-m', 'alembic', 'upgrade', 'head'])
  start(python, ['app.py'], backend)
  await waitUntil(async () => (await fetch('http://127.0.0.1:5311/health')).ok, 'Flask')
  server = staticServer()
  await waitUntil(async () => (await fetch('http://127.0.0.1:5312/')).ok, 'H5 server')

  const batch = await api('/api/intake-batches', { method: 'POST' })
  const pdf = path.join(work, 'three.pdf')
  runPython(['-c', 'from pypdf import PdfWriter; import sys; w=PdfWriter(); [w.add_blank_page(width=612,height=792) for _ in range(3)]; w.write(sys.argv[1])', pdf])
  const form = new FormData()
  form.append('file', new Blob([fs.readFileSync(pdf)], { type: 'application/pdf' }), 'three.pdf')
  await api(`/api/intake-batches/${batch.id}/files`, { method: 'POST', body: form })
  const items = await waitUntil(async () => {
    const result = await api(`/api/intake-batches/${batch.id}/items`)
    return result.total === 3 ? result.items : null
  }, 'three PDF pages')
  const move = await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'smoke-three', action: 'group',
    items: items.map(({ id, version }) => ({ id, version })),
    purchase_date: '2026-06-06', source_name: 'Smoke Market' }))
  assert(move.items.length === 3, 'move all pages')
  runPython(['-c', `import sqlite3,json,sys; c=sqlite3.connect(sys.argv[1]); ids=json.loads(sys.argv[2]);
for n,i in enumerate(ids,1): c.execute('UPDATE intake_items SET recognition_payload=? WHERE id=?',(json.dumps({'fields':{'invoice_number':{'status':'recognized','raw_text':f'INV-{n}'}}}),i))
c.commit()`, db, JSON.stringify(items.map((item) => item.id))])

  start(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-allow-origins=*',
    '--remote-debugging-port=9333', `--user-data-dir=${path.join(work, 'edge')}`,
    '--window-size=1280,900', `http://127.0.0.1:5312/?view=organize&batch=${batch.id}`])
  const target = await waitUntil(async () => {
    const pages = await (await fetch('http://127.0.0.1:9333/json/list')).json()
    return pages.find((page) => page.type === 'page' && page.url.includes('5312'))
  }, 'Edge CDP')
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  cdp = new Cdp(socket)
  await cdp.send('Runtime.enable')
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('开始填表'), 'organizer')
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('6 月 6 日'),
    `date group visible: ${await cdp.eval('document.body.innerText')}`)

  // The empty date is created by the user and can be deleted explicitly.
  // ⚠️ 新建日期自第 11 节 ② 起是三个格子，不再是一个 YYYY-MM-DD 文本框。
  await cdp.eval(`(() => { const boxes=[...document.querySelectorAll('.new-date input')];
    const fire=(box,value)=>{ box.value=value; box.dispatchEvent(new Event('input',{bubbles:true})) };
    fire(boxes[0],'2026'); fire(boxes[1],'06'); fire(boxes[2],'10'); return true })()`)
  await cdp.eval(`([...document.querySelectorAll('button,uni-button')].find(b=>b.textContent.includes('新建日期'))).click()`)
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('6 月 10 日'), 'new date')
  await cdp.eval(`(() => { const section=[...document.querySelectorAll('.section')]
    .find(s=>s.textContent.includes('6 月 10 日'));
    [...section.querySelectorAll('uni-button,button')]
      .find(b=>b.textContent.includes('删除类别')).click() })()`)
  await cdp.eval(`([...document.querySelectorAll('.overlay uni-button,.overlay button')]
    .find(b=>b.textContent.includes('删除类别'))).click()`)
  await waitUntil(async () => !(await api(`/api/intake-batches/${batch.id}/groups?suggested=true`)).groups
    .some((group) => group.purchase_date === '2026-06-10'), 'date deleted')

  // Drag a grouped page to the pending column; its explicit clear must beat
  // the date/supplier candidates, and the original date header must remain.
  const sendMouse = (type, point, buttons) => cdp.send('Input.dispatchMouseEvent', {
    type, x: Math.round(point.x), y: Math.round(point.y), button: 'left', buttons, clickCount: 1 })
  // Holding a card near the viewport edge must scroll the board without
  // another pointer movement. Escape cancels this probe before the real drop.
  const edgeDrag = await cdp.eval(`(() => { const filler=document.createElement('div');
    filler.id='drag-scroll-fixture'; filler.style.height='1500px'; document.body.appendChild(filler);
    const grip=[...document.querySelectorAll('.group .card .grip')][0];
    grip.scrollIntoView({block:'center'}); const rect=grip.getBoundingClientRect();
    return {from:{x:rect.x+rect.width/2,y:rect.y+rect.height/2},
      to:{x:rect.x+rect.width/2,y:innerHeight-12}, before:document.scrollingElement.scrollTop} })()`)
  await sendMouse('mousePressed', edgeDrag.from, 1)
  await sendMouse('mouseMoved', { x: edgeDrag.from.x + 15, y: edgeDrag.from.y + 15 }, 1)
  await sendMouse('mouseMoved', edgeDrag.to, 1)
  await sleep(350)
  const scrolled = await cdp.eval('document.scrollingElement.scrollTop')
  assert(scrolled > edgeDrag.before, `drag edge should scroll: ${edgeDrag.before} -> ${scrolled}`)
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await sendMouse('mouseReleased', edgeDrag.to, 0)
  await cdp.eval(`document.getElementById('drag-scroll-fixture').remove()`)
  const positions = await cdp.eval(`(() => { const grip=[...document.querySelectorAll('.group .card .grip')][0];
    grip.scrollIntoView({block:'center'}); const a=grip.getBoundingClientRect();
    const b=document.querySelector('.pending').getBoundingClientRect();
    return {from:{x:a.x+a.width/2,y:a.y+a.height/2},to:{x:b.x+b.width/2,y:b.y+Math.min(b.height/2,150)}} })()`)
  await sendMouse('mousePressed', positions.from, 1)
  await sendMouse('mouseMoved', { x: positions.from.x + 15, y: positions.from.y + 15 }, 1)
  await sendMouse('mouseMoved', positions.to, 1)
  await sendMouse('mouseReleased', positions.to, 0)
  const draggedId = items[0].id
  await waitUntil(async () => {
    const result = await api(`/api/intake-batches/${batch.id}/groups/items?suggested=true`)
    return result.items.some((item) => item.id === draggedId)
  }, 'dragged page in pending')
  assert((await api(`/api/intake-batches/${batch.id}/groups?suggested=true`)).groups
    .some((group) => group.purchase_date === '2026-06-06'), 'old date remains')
  const remaining = (await api(`/api/intake-batches/${batch.id}/items`)).items
    .filter((item) => item.id !== draggedId)
  await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'empty-sixth', action: 'clear',
    items: remaining.map(({ id, version }) => ({ id, version })) }))
  await cdp.send('Page.navigate', { url: `http://127.0.0.1:5312/?view=organize&batch=${batch.id}` })
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('这个日期暂无内容'),
    'empty date remains in browser')
  assert((await cdp.eval('document.body.innerText')).includes('6 月 6 日'), 'empty sixth remains')
  const current = (await api(`/api/intake-batches/${batch.id}/items`)).items
  await api(`/api/intake-batches/${batch.id}/moves`, json('POST', {
    request_key: 'restore-dragged', action: 'group',
    items: current.map(({ id, version }) => ({ id, version })),
    purchase_date: '2026-06-06', source_name: 'Smoke Market' }))
  await cdp.send('Page.navigate', { url: `http://127.0.0.1:5312/?view=organize&batch=${batch.id}` })
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('开始填表'), 'organizer reloaded')

  // ⚠️ 组上的按钮 2026-11-24 改名为「勾选多个文件填表」（第 13 节 ⑥）；卡片上那个
  //    「开始填表」是单张那条路，所以这里按整句匹配，不能用 includes('开始填表')。
  // Select page one and verify that review cannot navigate to pages two or three.
  const checkboxes = await cdp.eval(`[...document.querySelectorAll('[aria-label^="选中"]')]
    .filter(b=>b.getAttribute('aria-label').includes('three.pdf')).map(b=>b.getAttribute('aria-label'))`)
  assert(checkboxes.length >= 3, `expected three page checkboxes: ${JSON.stringify(checkboxes)}`)
  await cdp.eval(`([...document.querySelectorAll('[aria-label^="选中"]')]
    .find(b=>b.getAttribute('aria-label').includes('three.pdf'))).click()`)
  await cdp.eval(`([...document.querySelectorAll('button,uni-button')]
    .find(b=>b.innerText.trim()==='勾选多个文件填表' && !b.hasAttribute('disabled'))).click()`)
  await waitUntil(async () => (await cdp.eval('location.search')).includes('view=review'), 'review page')
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('本次选中 1 页'), 'one selected page')
  const controls = await cdp.eval(`(() => { const b=[...document.querySelectorAll('button,uni-button')];
    return {prev:b.find(x=>x.textContent==='上一页')?.hasAttribute('disabled'),
      next:b.find(x=>x.textContent==='下一页')?.hasAttribute('disabled'),
      text:document.body.innerText.includes('第 1 页 · 共选 1 页')} })()`)
  assert(controls.prev && controls.next && controls.text, `page restriction: ${JSON.stringify(controls)}`)

  await cdp.send('Page.navigate', { url: `http://127.0.0.1:5312/?view=organize&batch=${batch.id}` })
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('开始填表'), 'organizer again')
  await cdp.eval(`[...document.querySelectorAll('[aria-label^="选中"]')]
    .filter(b=>b.getAttribute('aria-label').includes('three.pdf')
      && (b.getAttribute('aria-label').includes('第 2 页') || b.getAttribute('aria-label').includes('第 3 页')))
    .forEach(b=>b.click())`)
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('已选 2 项'), 'two selected')
  await cdp.eval(`([...document.querySelectorAll('button,uni-button')]
    .find(b=>b.innerText.trim()==='勾选多个文件填表' && !b.hasAttribute('disabled'))).click()`)
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('本次选中 2 张单据'), 'two draft tabs')
  assert((await cdp.eval('document.body.innerText')).includes('本次选中 1 页'), 'first draft shows one page')
  await cdp.eval(`([...document.querySelectorAll('button,uni-button')].find(b=>b.textContent.includes('第 2 张 · 单据'))).click()`)
  await waitUntil(async () => (await cdp.eval('document.body.innerText')).includes('第 3 页 · 共选 1 页'),
    'second draft restricted to page three')
  console.log(JSON.stringify({ passed: true, batch_id: batch.id, selected_page: 1,
    page_controls: controls, date_created_and_deleted: '2026-06-10',
    dragged_to_pending: draggedId, distinct_invoices: 2 }))
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
    try { fs.rmSync(target, { recursive: true, force: true, maxRetries: 10, retryDelay: 300 }) }
    catch (error) { console.warn(`Smoke cleanup could not remove ${target}: ${error.message}`) }
  } else {
    throw new Error(`Refusing to remove unexpected smoke directory: ${target}`)
  }
}
