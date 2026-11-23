export type WorkspaceView = 'home' | 'upload' | 'batch' | 'organize' | 'ledger' | 'invoices' | 'review' | 'ingredients'

export interface WorkspaceLocation {
  view: WorkspaceView
  invoiceId: number | null
  batchId?: number | null
  foodItemId?: number | null
  ingredientTab?: 'catalog' | 'prices'
}

const fallback: WorkspaceLocation = { view: 'home', invoiceId: null, foodItemId: null }

/** Keep browser history details outside business components. App back handling is a later adapter task. */
export function readWorkspaceLocation(): WorkspaceLocation {
  // #ifdef H5
  const params = new URLSearchParams(window.location.search)
  const requested = params.get('view')
  const view: WorkspaceView = requested === 'upload' || requested === 'batch' || requested === 'organize' || requested === 'ledger' || requested === 'ingredients' || requested === 'review' || requested === 'invoices' || requested === 'prices'
    ? requested === 'prices' ? 'ingredients' : requested
    : 'home'
  const rawId = params.get('invoice')
  const invoiceId = rawId && /^[1-9]\d*$/.test(rawId) && Number.isSafeInteger(Number(rawId))
    ? Number(rawId)
    : null
  const rawFoodId = params.get('food_item')
  const rawBatchId = params.get('batch')
  const batchId = rawBatchId && /^[1-9]\d*$/.test(rawBatchId) && Number.isSafeInteger(Number(rawBatchId))
    ? Number(rawBatchId) : null
  const foodItemId = rawFoodId && /^[1-9]\d*$/.test(rawFoodId) && Number.isSafeInteger(Number(rawFoodId))
    ? Number(rawFoodId)
    : null
  return {
    view,
    invoiceId: view === 'review' ? invoiceId : null,
    batchId: view === 'batch' || view === 'organize' || view === 'ledger' ? batchId : null,
    foodItemId: view === 'ingredients' ? foodItemId : null,
    ingredientTab: view === 'ingredients' && (requested === 'prices' || params.get('tab') === 'prices') ? 'prices' : 'catalog'
  }
  // #endif
  return fallback
}

export function writeWorkspaceLocation(location: WorkspaceLocation, replace = false): void {
  // #ifdef H5
  const next = new URL(window.location.href)
  if (location.view === 'home') next.searchParams.delete('view')
  else next.searchParams.set('view', location.view)
  if (location.view === 'review' && location.invoiceId !== null) {
    next.searchParams.set('invoice', String(location.invoiceId))
  } else {
    next.searchParams.delete('invoice')
  }
  if ((location.view === 'batch' || location.view === 'organize' || location.view === 'ledger') && location.batchId) {
    next.searchParams.set('batch', String(location.batchId))
  } else {
    next.searchParams.delete('batch')
  }
  if (location.view === 'ingredients' && location.foodItemId) next.searchParams.set('food_item', String(location.foodItemId))
  else next.searchParams.delete('food_item')
  if (location.view === 'ingredients' && location.ingredientTab === 'prices') next.searchParams.set('tab', 'prices')
  else next.searchParams.delete('tab')
  const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`
  const nextPath = `${next.pathname}${next.search}${next.hash}`
  if (currentPath === nextPath) return
  window.history[replace ? 'replaceState' : 'pushState']({}, '', nextPath)
  // #endif
}

export function onWorkspaceHistoryChange(callback: () => void): () => void {
  // #ifdef H5
  window.addEventListener('popstate', callback)
  return () => window.removeEventListener('popstate', callback)
  // #endif
  return () => {}
}
