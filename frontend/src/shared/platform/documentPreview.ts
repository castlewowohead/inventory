/**
 * Turning stored bytes into something a page can show — isolated from the
 * invoice components.
 *
 * Why an adapter: every call below is a browser API (`URL.createObjectURL`,
 * `Image`, `<canvas>`, and pdf.js, which itself needs a Worker and a canvas).
 * None of it exists on App, so the review screen must not reach for it
 * directly. The business components only ever see `RenderedPage` and
 * `PdfDocument`.
 *
 * ### H5-only, and where the App work sits
 *
 * | Capability | H5 | App |
 * | --- | --- | --- |
 * | image preview | object URL + `<image>` | needs a local temp file written by `uni.saveFile`, then `src` is that path |
 * | HEIC | only when the browser itself decodes it — probed, never assumed | iOS decodes HEIC natively; Android webviews usually do not |
 * | PDF page rendering | pdf.js renders into a `<canvas>` | pdf.js has no canvas there; use the platform PDF viewer or a native plugin |
 * | page count | pdf.js | same as above |
 *
 * So the App task is not "port this file" but "answer the same four questions
 * with platform APIs". The non-H5 branches below therefore throw a message that
 * names the missing capability instead of silently showing nothing.
 *
 * ### Browser baseline
 *
 * pdf.js 4.10 is built for current browsers (it uses `Promise.withResolvers`,
 * Chrome 119+ / Safari 17.4+). This is a local-demo front end, so that baseline
 * is acceptable; a wider one means switching to `pdfjs-dist/legacy`.
 */

// ⚠️ pdf.js is imported **statically**, not with `await import(...)`, and that is
// not a style choice. `uni build -p h5` (vite-plugin-uni 3.0.0-alpha-5030620261106001)
// turns a dynamic import of a node_modules package into the bare specifier
// `import("..-node_modules-pdfjs-dist-build-pdf.<hash>.js")` and never emits that
// chunk, so the built page dies with "Failed to resolve module specifier" while
// `npm run dev:h5` works fine. Verified on this project's build; the two
// conditional-compilation blocks keep pdf.js out of non-H5 builds, which is what
// the dynamic import was for.
// #ifdef H5
import * as pdfjs from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
// #endif

export type DocumentKind = 'image' | 'pdf' | 'unknown'

export class PreviewUnavailableError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PreviewUnavailableError'
  }
}

/** Something the page can point an `<image>` at, plus its natural size. */
export interface RenderedPage {
  url: string
  width: number
  height: number
  /** Frees the object URL. Always call it before dropping the reference. */
  release: () => void
}

export interface PdfDocument {
  pageCount: number
  /**
   * Renders one page at roughly `targetWidth` device pixels wide. The caller
   * scales the result with CSS, so this only decides how sharp it is.
   */
  renderPage: (pageNumber: number, targetWidth: number) => Promise<RenderedPage>
  close: () => void
}

const IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic']

function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot).toLowerCase() : ''
}

/**
 * What kind of document this file is.
 *
 * The media type the server stored wins; a browser that handed over no type at
 * all (HEIC from a desktop file input is stored as
 * `application/octet-stream`) leaves only the extension to go on.
 */
export function classifyDocument(name: string, mediaType: string): DocumentKind {
  const type = (mediaType || '').split(';')[0].trim().toLowerCase()
  if (type === 'application/pdf') return 'pdf'
  if (type.startsWith('image/')) return 'image'
  const extension = extensionOf(name)
  if (extension === '.pdf') return 'pdf'
  if (IMAGE_EXTENSIONS.includes(extension)) return 'image'
  return 'unknown'
}

/** True when this extension is one the browser is expected to struggle with. */
export function isHeic(name: string, mediaType: string): boolean {
  const type = (mediaType || '').split(';')[0].trim().toLowerCase()
  return extensionOf(name) === '.heic' || type === 'image/heic' || type === 'image/heif'
}

// #ifdef H5
function objectUrl(bytes: ArrayBuffer, mediaType: string): string {
  return URL.createObjectURL(new Blob([bytes], { type: mediaType || 'application/octet-stream' }))
}

/**
 * Waits for the browser to actually decode the image.
 *
 * ⚠️ This is the only honest way to answer "can this browser preview a HEIC?".
 * Chrome on Windows cannot, Safari can, and the file extension says nothing
 * about the browser in front of us — so the page asks the browser rather than
 * promising on its behalf.
 */
function decodeImage(url: string): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve({
      width: image.naturalWidth || 0,
      height: image.naturalHeight || 0
    })
    image.onerror = () => reject(new Error('decode failed'))
    image.src = url
  })
}

function canvasToUrl(canvas: HTMLCanvasElement): Promise<string> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(URL.createObjectURL(blob))
      else reject(new PreviewUnavailableError('浏览器没能把这一页转成图片。'))
    }, 'image/png')
  })
}
// #endif

/**
 * An image file, ready to show.
 *
 * Throws `PreviewUnavailableError` when the browser refuses to decode it; the
 * stored original is untouched either way, which is what the message says.
 */
export async function renderImage(
  bytes: ArrayBuffer,
  mediaType: string,
  name: string
): Promise<RenderedPage> {
  // #ifdef H5
  const url = objectUrl(bytes, mediaType)
  try {
    const size = await decodeImage(url)
    return { url, width: size.width, height: size.height, release: () => URL.revokeObjectURL(url) }
  } catch {
    URL.revokeObjectURL(url)
    throw new PreviewUnavailableError(
      isHeic(name, mediaType)
        ? `无法预览 ${name}。原件已保存；请换用支持 HEIC 的设备查看，或上传 JPG、PNG 格式。`
        : `无法预览 ${name}。原件已保存，可下载后查看。`
    )
  }
  // #endif
  throw new PreviewUnavailableError(
    '当前设备暂时无法预览图片，请下载原件查看。'
  )
}

/**
 * A legible card preview: draw a moderate enlargement of the page's upper
 * portion into a fixed frame. The original RenderedPage remains the caller's
 * responsibility; this returns a separate small bitmap URL to release.
 */
export async function cropReadablePreview(
  page: RenderedPage, width = 700, height = 600, zoom = 1.85
): Promise<RenderedPage> {
  // #ifdef H5
  const image = new Image()
  image.src = page.url
  await image.decode()
  const sourceWidth = image.naturalWidth || page.width
  const sourceHeight = image.naturalHeight || page.height
  if (!sourceWidth || !sourceHeight) throw new PreviewUnavailableError('原件没有可预览的图像内容。')
  const fit = Math.min(width / sourceWidth, height / sourceHeight)
  const cover = Math.max(width / sourceWidth, height / sourceHeight)
  const scale = Math.max(fit * zoom, cover)
  const cropWidth = Math.min(sourceWidth, width / scale)
  const cropHeight = Math.min(sourceHeight, height / scale)
  const canvas = window.document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext('2d')
  if (!context) throw new PreviewUnavailableError('浏览器没有可用的 canvas 2d 上下文。')
  context.fillStyle = '#fff'
  context.fillRect(0, 0, width, height)
  context.drawImage(image, (sourceWidth - cropWidth) / 2, 0, cropWidth, cropHeight, 0, 0, width, height)
  const url = await canvasToUrl(canvas)
  return { url, width, height, release: () => URL.revokeObjectURL(url) }
  // #endif
  throw new PreviewUnavailableError('当前设备暂时无法生成原件缩略图。')
}

/** Opens a PDF and keeps it open so page switching does not re-parse the file. */
export async function openPdf(bytes: ArrayBuffer): Promise<PdfDocument> {
  // #ifdef H5
  pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl
  // ⚠️ pdf.js takes ownership of the buffer it is given (it ends up detached),
  // so hand it a copy — the caller caches the original bytes and would
  // otherwise find them emptied the next time it wants this file.
  const document = await pdfjs.getDocument({
    data: new Uint8Array(bytes.slice(0)),
    isEvalSupported: false
  }).promise
  return {
    pageCount: document.numPages,
    renderPage: async (pageNumber: number, targetWidth: number) => {
      const page = await document.getPage(pageNumber)
      const base = page.getViewport({ scale: 1 })
      const scale = base.width > 0 ? Math.max(0.2, targetWidth / base.width) : 1
      const viewport = page.getViewport({ scale })
      const canvas = window.document.createElement('canvas')
      canvas.width = Math.max(1, Math.floor(viewport.width))
      canvas.height = Math.max(1, Math.floor(viewport.height))
      const context = canvas.getContext('2d')
      if (!context) throw new PreviewUnavailableError('浏览器没有可用的 canvas 2d 上下文。')
      await page.render({ canvasContext: context, viewport }).promise
      const url = await canvasToUrl(canvas)
      page.cleanup()
      return {
        url,
        width: canvas.width,
        height: canvas.height,
        release: () => URL.revokeObjectURL(url)
      }
    },
    close: () => { void document.destroy() }
  }
  // #endif
  throw new PreviewUnavailableError(
    'App 端还没有 PDF 预览适配器：pdf.js 在那里没有 canvas，要改用平台的 PDF 组件或原生插件。'
  )
}

/**
 * How many pages a **not yet uploaded** file has, or `null` when this platform
 * or this file cannot answer.
 *
 * The upload entry uses it to tell one document from a stack of them: a single
 * one-page PDF or one photo goes straight to the fill page, anything else goes
 * to the organizing board (`docs/multifiles.md` 第 11 节 ①). ⚠️ `null` is not
 * "zero pages" — it means **unknown**, and the caller must then take the
 * organizing route, which handles any file the server accepts.
 */
export async function countLocalPages(
  source: File | string, name: string, mediaType: string
): Promise<number | null> {
  const kind = classifyDocument(name, mediaType)
  if (kind === 'image') return 1
  if (kind !== 'pdf') return null
  // #ifdef H5
  if (typeof File === 'undefined' || !(source instanceof File)) return null
  let document: PdfDocument | null = null
  try {
    document = await openPdf(await source.arrayBuffer())
    return document.pageCount
  } catch {
    // A PDF the browser cannot parse is not a reason to block the upload; the
    // server still takes the bytes and reports what it found.
    return null
  } finally {
    document?.close()
  }
  // #endif
  return null
}
