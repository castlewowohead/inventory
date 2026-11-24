/**
 * Pointer dragging for the intake organizing board (M10), isolated from the
 * business components the way `documentPreview.ts` isolates pdf.js.
 *
 * ### Why an adapter
 *
 * Every call below is a browser API — `PointerEvent`, `window` listeners,
 * `document.elementFromPoint`. None of it exists on App, so the organizer must
 * not reach for it directly. The component only ever sees a drop-target key
 * and a pointer position; `boardDragSupported()` answers whether dragging is
 * available at all, and the board keeps working through its buttons when it is
 * not. Dragging is a shortcut, never the only way to move a card
 * (`docs/multifiles.md` section 4, W3C G219).
 *
 * ### Why pointer events and not HTML5 drag-and-drop
 *
 * `dragstart`/`dragover` do not fire for touch at all, so a phone would be left
 * with no drag at all and the code with two separate implementations. Pointer
 * events cover mouse and touch in one path, which is also what lets the same
 * session drive the stacked preview the plan asks for.
 *
 * ### Why the handle is bound here and not with `@pointerdown`
 *
 * ⚠️ Measured in Edge on the built H5 bundle: uni-app's `<view>` forwards only
 * the events it knows about. `@click` arrives, `@pointerdown` **never does** —
 * the listener is dropped silently, with no warning and no error, so a grip
 * bound in the template simply does nothing. `attachDragHandle()` therefore
 * takes the element (or the uni-app component whose `$el` it is) and adds a
 * real DOM listener. That also keeps `PointerEvent` out of the components.
 *
 * ### How touch scrolling keeps winning
 *
 * The caller puts this on a **small, visible grip** and gives that grip
 * `touch-action: none` (Atlassian's guidance, cited in section 2: show the
 * draggable area). Only that grip stops being scrollable; a finger anywhere
 * else on the card still scrolls the list, and the browser cancels an
 * in-progress session with `pointercancel` if it decides to scroll anyway.
 * A `MOVE_THRESHOLD` of a few pixels keeps a plain tap on the grip from
 * counting as a drag.
 */

/** Set by the caller on every drop zone; the value is the target's key. */
export const DROP_KEY_ATTRIBUTE = 'data-drop-key'

export interface DragPosition {
  x: number
  y: number
}

export interface BoardDragOptions {
  /** The `pointerdown` event from the grip, as `attachDragHandle` passes it on. */
  event: unknown
  /** Fires once the pointer has really moved, not on the initial press. */
  onStart?: () => void
  onMove?: (position: DragPosition, targetKey: string | null) => void
  /** A key means it was dropped there; `null` means cancelled or dropped nowhere. */
  onEnd: (targetKey: string | null) => void
}

/** Px the pointer must travel before a press becomes a drag. */
const MOVE_THRESHOLD = 6
const SCROLL_EDGE = 72

let active: (() => void) | null = null

export function boardDragSupported(): boolean {
  return typeof window !== 'undefined' && typeof document !== 'undefined'
    && typeof window.PointerEvent === 'function' && typeof document.elementFromPoint === 'function'
}

function targetAt(x: number, y: number): string | null {
  const element = document.elementFromPoint(x, y)
  if (!(element instanceof Element)) return null
  const zone = element.closest(`[${DROP_KEY_ATTRIBUTE}]`)
  return zone?.getAttribute(DROP_KEY_ATTRIBUTE) ?? null
}

/**
 * Starts one drag session. Returns false when this press cannot start one —
 * the caller then leaves the press alone instead of pretending to drag.
 */
export function startBoardDrag(options: BoardDragOptions): boolean {
  if (!boardDragSupported()) return false
  const origin = options.event
  if (!(origin instanceof PointerEvent) || !origin.isPrimary) return false
  // Right and middle buttons belong to the browser's own menus.
  if (origin.pointerType === 'mouse' && origin.button !== 0) return false
  active?.()

  const pointerId = origin.pointerId
  const startX = origin.clientX
  const startY = origin.clientY
  let started = false
  let lastTarget: string | null = null
  let lastPosition: DragPosition | null = null
  let scrollFrame = 0
  const previousUserSelect = document.body.style.userSelect

  function scrollAtEdge() {
    scrollFrame = 0
    if (!started || !lastPosition) return
    const { x, y } = lastPosition
    const hit = document.elementFromPoint(x, y)
    let scroller: Element | null = hit
    while (scroller && scroller !== document.body) {
      const style = window.getComputedStyle(scroller)
      if (/(auto|scroll)/.test(style.overflowY) && scroller.scrollHeight > scroller.clientHeight + 1) break
      scroller = scroller.parentElement
    }
    if (!scroller || scroller === document.body) scroller = document.scrollingElement
    if (scroller) {
      const page = scroller === document.scrollingElement
      const rect = page ? { top: 0, bottom: window.innerHeight } : scroller.getBoundingClientRect()
      const topDistance = y - rect.top
      const bottomDistance = rect.bottom - y
      const direction = topDistance < SCROLL_EDGE ? -1 : bottomDistance < SCROLL_EDGE ? 1 : 0
      if (direction) {
        const distance = direction < 0 ? topDistance : bottomDistance
        const speed = Math.max(4, Math.round((SCROLL_EDGE - Math.max(0, distance)) / 4))
        scroller.scrollTop += direction * speed
        lastTarget = targetAt(x, y)
        options.onMove?.({ x, y }, lastTarget)
      }
    }
    scrollFrame = window.requestAnimationFrame(scrollAtEdge)
  }

  function finish(targetKey: string | null) {
    stop()
    options.onEnd(targetKey)
  }

  function onPointerMove(event: PointerEvent) {
    if (event.pointerId !== pointerId) return
    if (!started) {
      if (Math.abs(event.clientX - startX) < MOVE_THRESHOLD
        && Math.abs(event.clientY - startY) < MOVE_THRESHOLD) return
      started = true
      document.body.style.userSelect = 'none'
      options.onStart?.()
    }
    // Only now, with a real drag under way, is the browser's own gesture
    // (text selection, or a scroll the grip's `touch-action` did not rule out)
    // worth taking over.
    event.preventDefault()
    lastPosition = { x: event.clientX, y: event.clientY }
    lastTarget = targetAt(event.clientX, event.clientY)
    options.onMove?.({ x: event.clientX, y: event.clientY }, lastTarget)
    if (!scrollFrame) scrollFrame = window.requestAnimationFrame(scrollAtEdge)
  }

  function onPointerUp(event: PointerEvent) {
    if (event.pointerId !== pointerId) return
    finish(started ? targetAt(event.clientX, event.clientY) : null)
  }

  function onPointerCancel(event: PointerEvent) {
    if (event.pointerId !== pointerId) return
    finish(null)
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === 'Escape') finish(null)
  }

  function stop() {
    if (active !== stop) return
    active = null
    if (scrollFrame) window.cancelAnimationFrame(scrollFrame)
    scrollFrame = 0
    document.body.style.userSelect = previousUserSelect
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    window.removeEventListener('pointercancel', onPointerCancel)
    window.removeEventListener('keydown', onKeyDown)
  }

  active = stop
  window.addEventListener('pointermove', onPointerMove, { passive: false })
  window.addEventListener('pointerup', onPointerUp)
  window.addEventListener('pointercancel', onPointerCancel)
  window.addEventListener('keydown', onKeyDown)
  return true
}

/** Drops whatever session is running, e.g. when the board unmounts. */
export function cancelBoardDrag(): void {
  active?.()
}

/** What a uni-app template ref looks like once it is a real element. */
function elementOf(target: unknown): HTMLElement | null {
  if (typeof HTMLElement !== 'undefined' && target instanceof HTMLElement) return target
  const host = (target as { $el?: unknown } | null)?.$el
  return typeof HTMLElement !== 'undefined' && host instanceof HTMLElement ? host : null
}

/**
 * Binds a drag grip. Returns the detach function, or a no-op when this
 * platform has no pointer events — the caller's buttons still work.
 */
export function attachDragHandle(target: unknown, onPress: (event: unknown) => void): () => void {
  const element = elementOf(target)
  if (!element || !boardDragSupported()) return () => {}
  const handler = (event: Event) => onPress(event)
  element.addEventListener('pointerdown', handler)
  return () => element.removeEventListener('pointerdown', handler)
}
