export interface FocusableWindow {
  isMinimized(): boolean
  restore(): void
  show(): void
  focus(): void
}

export function focusExistingWindow(window: FocusableWindow | null): void {
  if (!window) return
  if (window.isMinimized()) window.restore()
  window.show()
  window.focus()
}
