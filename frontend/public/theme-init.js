// Runs before first paint so the page never flashes the wrong palette.
// Mirrors resolveTheme/applyResolvedTheme in src/lib/theme/appearance.ts.
(() => {
  const storageKey = "newsroom-ui"
  const themeColors = { light: "#ffffff", dark: "#161616" }
  let preference = "system"
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || "null")
    const value = stored && stored.state && stored.state.appearance
    if (value === "system" || value === "light" || value === "dark") preference = value
  } catch {}
  const systemLight = typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches
  const theme = preference === "system" ? (systemLight ? "light" : "dark") : preference
  const root = document.documentElement
  root.dataset.theme = theme
  root.style.colorScheme = theme
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute("content", themeColors[theme])
})()
