/**
 * "You have unsaved input" guard (plan section 6: 未提交输入离开时提示).
 *
 * The workspace container asks this module before switching views, and the H5
 * adapter hooks `beforeunload` so a refresh or a closed tab asks too. A
 * component registers a function that returns a reason while it holds unsaved
 * input, and `null` once it does not. Keeping the browser API here leaves the
 * business components free of DOM calls, which is what the App adapter task
 * needs.
 *
 * `onDiscard` runs when the user chose to leave anyway: without it a component
 * that stays mounted (a hidden view) would keep asking about input the user has
 * already agreed to drop.
 */

interface Guard {
  reason: () => string | null
  discard?: () => void
}

const guards = new Set<Guard>()

/** Register while a component may hold unsaved input. Call the result to unregister. */
export function registerLeaveGuard(reason: () => string | null, onDiscard?: () => void): () => void {
  const guard: Guard = { reason, discard: onDiscard }
  guards.add(guard)
  return () => guards.delete(guard)
}

/** The first pending reason, or null when nothing is dirty. */
export function pendingLeaveReason(): string | null {
  for (const guard of guards) {
    const reason = guard.reason()
    if (reason) return reason
  }
  return null
}

function discardAll(): void {
  for (const guard of guards) {
    if (guard.reason()) guard.discard?.()
  }
}

/**
 * Ask the user before leaving. Resolves true when it is safe to continue, and
 * tells the dirty components to drop their input in that case.
 * No pending input means no dialog at all.
 */
export function confirmLeave(): Promise<boolean> {
  const reason = pendingLeaveReason()
  if (!reason) return Promise.resolve(true)
  return new Promise((resolve) => {
    uni.showModal({
      title: '还有未保存的内容',
      content: reason,
      confirmText: '仍要离开',
      cancelText: '留在本页',
      confirmColor: '#9a3b2f',
      success: (result) => {
        const leave = Boolean(result.confirm)
        if (leave) discardAll()
        resolve(leave)
      },
      // A dialog that cannot open must not turn into a silent discard.
      fail: () => resolve(false)
    })
  })
}

/** Browser refresh / tab close. H5 only; returns a cleanup function. */
export function watchPageUnload(): () => void {
  // #ifdef H5
  const handler = (event: BeforeUnloadEvent) => {
    if (!pendingLeaveReason()) return
    // The browser shows its own wording; a prevented default triggers it.
    event.preventDefault()
    event.returnValue = ''
  }
  window.addEventListener('beforeunload', handler)
  return () => window.removeEventListener('beforeunload', handler)
  // #endif
  return () => {}
}
