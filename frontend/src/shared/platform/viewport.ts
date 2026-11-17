/**
 * Viewport width, isolated from the invoice components.
 *
 * Why this exists instead of a CSS media query alone: the review screen has to
 * *unmount nothing* and still show one pane at a time on a phone, so the pane
 * switch is component state, and component state needs to know whether the
 * layout is narrow. Driving both the layout classes and that state from one
 * number keeps them from disagreeing at the breakpoint — a CSS query and a JS
 * check can round the width differently, and the result would be a desktop with
 * one pane hidden.
 *
 * `uni.getSystemInfoSync` and `uni.onWindowResize` are available on H5 and App,
 * so no platform branch is needed here.
 */

/** Matches the 900 CSS px breakpoint in the plan (section 3). */
export const NARROW_MAX_WIDTH = 899

export function isNarrowViewport(): boolean {
  const width = uni.getSystemInfoSync().windowWidth
  return typeof width === 'number' && width > 0 ? width <= NARROW_MAX_WIDTH : false
}

/** Calls back on every resize. Returns the unsubscribe function. */
export function watchViewport(onChange: (narrow: boolean) => void): () => void {
  const handler = () => onChange(isNarrowViewport())
  uni.onWindowResize(handler)
  return () => uni.offWindowResize(handler)
}
