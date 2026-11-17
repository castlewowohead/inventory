/** uni-app H5 renders <button> as <uni-button>, which is not a native tab stop. */
export function installH5KeyboardButtons(): void {
  if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') return

  function prepare(button: Element): void {
    if (button.tagName !== 'UNI-BUTTON') return
    const disabled = button.hasAttribute('disabled')
    button.setAttribute('tabindex', disabled ? '-1' : '0')
    button.setAttribute('role', 'button')
    button.setAttribute('aria-disabled', String(disabled))
  }

  function prepareTree(node: Node): void {
    if (!(node instanceof Element)) return
    prepare(node)
    node.querySelectorAll('uni-button').forEach(prepare)
  }

  prepareTree(document.body)
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') prepare(record.target as Element)
      else record.addedNodes.forEach(prepareTree)
    }
  })
  observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['disabled'] })

  document.addEventListener('keydown', (event) => {
    const target = event.target
    if (!(target instanceof HTMLElement) || target.tagName !== 'UNI-BUTTON') return
    if (target.hasAttribute('disabled') || event.repeat || event.isComposing) return
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    target.click()
  })
}
