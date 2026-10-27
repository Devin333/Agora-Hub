import { afterEach, describe, expect, it, vi } from "vitest"
import {
  isModelCredentialFailure,
  openSettings,
  openSettingsEvent,
  parseSettingsSection,
  settingsHref,
  settingsSectionFrom,
} from "./settings-navigation"

describe("settings navigation", () => {
  afterEach(() => vi.restoreAllMocks())

  it("lets the workbench that owns the dialog claim the request", () => {
    const navigate = vi.fn()
    const claim = (event: Event) => {
      event.preventDefault()
      expect(settingsSectionFrom(event)).toBe("model")
    }
    window.addEventListener(openSettingsEvent, claim)
    try {
      openSettings("model", navigate)
    } finally {
      window.removeEventListener(openSettingsEvent, claim)
    }
    expect(navigate).not.toHaveBeenCalled()
  })

  it("sends the user to the workbench when no page claims the request", () => {
    const navigate = vi.fn()
    openSettings("model", navigate)
    expect(navigate).toHaveBeenCalledWith("/paper-agent?settings=model")
  })

  it("builds and parses only known sections", () => {
    expect(settingsHref("admin")).toBe("/paper-agent?settings=admin")
    expect(parseSettingsSection("network")).toBe("network")
    expect(parseSettingsSection("toString")).toBeNull()
    expect(parseSettingsSection("")).toBeNull()
    expect(parseSettingsSection(null)).toBeNull()
    expect(settingsSectionFrom(new CustomEvent(openSettingsEvent, { detail: { section: "../admin" } }))).toBe("account")
    expect(settingsSectionFrom(new Event(openSettingsEvent))).toBe("account")
  })

  it("recognizes exactly the failures fixed by the account's own model key", () => {
    for (const code of ["model_credential_required", "model_credential_key_rotated", "model_credential_rejected"]) {
      expect(isModelCredentialFailure(code)).toBe(true)
    }
    for (const code of ["model_provider_unreachable", "research_parse_unavailable", "", null, undefined]) {
      expect(isModelCredentialFailure(code)).toBe(false)
    }
  })
})
