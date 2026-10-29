import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { openSettingsEvent, settingsSectionFrom } from "@/lib/account/settings-navigation"
import { ModelCredentialAction } from "./model-credential-action"

describe("ModelCredentialAction", () => {
  afterEach(cleanup)

  it("renders nothing for failures a model key does not fix", () => {
    const { container } = render(<><ModelCredentialAction code="model_provider_unreachable" /><ModelCredentialAction code={null} /></>)
    expect(container).toBeEmptyDOMElement()
  })

  it.each([
    ["model_credential_required", "填写模型 API Key"],
    ["model_credential_key_rotated", "重新填写模型 API Key"],
    ["model_credential_rejected", "重新填写模型 API Key"],
  ])("opens the model settings section for %s", (code, label) => {
    const claim = vi.fn((event: Event) => {
      event.preventDefault()
      return settingsSectionFrom(event)
    })
    window.addEventListener(openSettingsEvent, claim)
    try {
      render(<ModelCredentialAction code={code} />)
      fireEvent.click(screen.getByRole("button", { name: label }))
    } finally {
      window.removeEventListener(openSettingsEvent, claim)
    }
    expect(claim).toHaveBeenCalledOnce()
    expect(claim.mock.results[0].value).toBe("model")
  })
})
