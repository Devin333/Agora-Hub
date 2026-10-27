"use client"

import { useSyncExternalStore } from "react"
import { resolveTheme, systemPrefersLight, watchSystemAppearance, type ResolvedTheme } from "@/lib/theme/appearance"
import { useUiStore } from "@/stores/ui-store"

function subscribeToSystemAppearance(onChange: () => void): () => void {
  return watchSystemAppearance(onChange)
}

function readServerPreference(): boolean | null {
  return null
}

/**
 * The app-wide appearance preference resolved to a palette for the preview root.
 *
 * `/paper-agent-lab` is kept outside ThemeBridge's document side effects (see
 * `components/providers.tsx`), so the preview scopes the same resolved palette to its
 * own root through `data-theme` instead of writing `html[data-theme]`. It returns
 * `undefined` on the server and while hydrating, where the root inherits the palette
 * `public/theme-init.js` already applied from the stored preference.
 */
export function usePreviewAppearance(): ResolvedTheme | undefined {
  const appearance = useUiStore((state) => state.appearance)
  const prefersLight = useSyncExternalStore(subscribeToSystemAppearance, systemPrefersLight, readServerPreference)
  return prefersLight === null ? undefined : resolveTheme(appearance, prefersLight)
}
