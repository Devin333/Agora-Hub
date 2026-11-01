import { afterEach, describe, expect, it, vi } from "vitest"
import {
  isModelConnectionFailure,
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

  it("recognizes exactly the failures fixed in the model connection settings", () => {
    for (const code of [
      "model_connection_required",
      "model_connection_key_missing",
      "model_connection_key_rotated",
      "model_connection_rejected",
      "model_connection_withdrawn",
      "model_connection_window_too_small",
      "model_connection_structured_output_required",
    ]) {
      expect(isModelConnectionFailure(code)).toBe(true)
    }
    for (const code of [
      "model_provider_unreachable",
      "model_credential_required",
      "research_parse_unavailable",
      "",
      null,
      undefined,
    ]) {
      expect(isModelConnectionFailure(code)).toBe(false)
    }
  })
})
