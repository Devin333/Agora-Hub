import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { openSettingsEvent, settingsSectionFrom } from "@/lib/account/settings-navigation"
import { ModelConnectionAction } from "./model-connection-action"

describe("ModelConnectionAction", () => {
  afterEach(cleanup)

  it("renders nothing for failures the connection settings do not fix", () => {
    const { container } = render(<><ModelConnectionAction code="model_provider_unreachable" /><ModelConnectionAction code={null} /></>)
    expect(container).toBeEmptyDOMElement()
  })

  it.each([
    ["model_connection_required", "设置模型连接"],
    ["model_connection_window_too_small", "设置模型连接"],
    ["model_connection_withdrawn", "设置模型连接"],
    ["model_connection_key_missing", "重新填写 API Key"],
    ["model_connection_key_rotated", "重新填写 API Key"],
    ["model_connection_rejected", "重新填写 API Key"],
  ])("opens the model settings section for %s", (code, label) => {
    const claim = vi.fn((event: Event) => {
      event.preventDefault()
      return settingsSectionFrom(event)
    })
    window.addEventListener(openSettingsEvent, claim)
    try {
      render(<ModelConnectionAction code={code} />)
      fireEvent.click(screen.getByRole("button", { name: label }))
    } finally {
      window.removeEventListener(openSettingsEvent, claim)
    }
    expect(claim).toHaveBeenCalledOnce()
    expect(claim.mock.results[0].value).toBe("model")
  })
})
