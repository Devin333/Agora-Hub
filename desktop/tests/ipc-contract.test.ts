import { describe, expect, it } from "vitest"
import { IPC_CHANNELS } from "../src/channels"
import { validateConfigCandidate } from "../src/desktop-config-bridge"

describe("preload contract", () => {
  it("contains only the exact enumerated native channels", () => {
    expect(Object.keys(IPC_CHANNELS).sort()).toEqual([
      "appearanceSet", "configApply", "configPreview", "configShow", "credentialRemove", "credentialReplace", "credentialStatus",
      "exportSave", "lifecycleQuitRequested", "lifecycleRespond", "lifecycleUpdate", "runtimeBackup",
      "runtimeClearDataApply", "runtimeClearDataPreview", "runtimeClearDataStatus", "runtimeDiagnostics", "runtimeRestoreActivate", "runtimeRestorePreview", "runtimeRetry", "runtimeStatus"
    ])
    expect(Object.values(IPC_CHANNELS).sort()).toEqual([
      "agora:appearance:set", "agora:config:apply", "agora:config:preview", "agora:config:show", "agora:credential:remove",
      "agora:credential:replace", "agora:credential:status", "agora:export:save",
      "agora:lifecycle:quit-requested", "agora:lifecycle:respond", "agora:lifecycle:update",
      "agora:runtime:backup", "agora:runtime:clear-data:apply", "agora:runtime:clear-data:preview",
      "agora:runtime:clear-data:status", "agora:runtime:diagnostics", "agora:runtime:restore:activate",
      "agora:runtime:restore:preview", "agora:runtime:retry", "agora:runtime:status"
    ])
    expect(Object.values(IPC_CHANNELS).filter(channel => /shell|exec|read-file|write-file|path/i.test(channel))).toEqual([])
  })

  it("accepts only exact config candidate namespaces and bounded fields", () => {
    expect(validateConfigCandidate({
      research: {
        llm: null,
        source: { apiUrl: "https://export.arxiv.org/api/query?search_query=all", timeoutSeconds: 30 }
      }
    })).toEqual({
      llm: null,
      source: { api_url: "https://export.arxiv.org/api/query?search_query=all", timeout_seconds: 30 }
    })

    const invalid = [
      {},
      { research: { llm: null, source: { apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 30 }, shell: "cmd.exe" } },
      { research: { llm: null, source: { apiUrl: "file:///C:/secret", timeoutSeconds: 30 } } },
      { research: { llm: null, source: { apiUrl: "https://user:pass@example.test/api", timeoutSeconds: 30 } } },
      { research: { llm: null, source: { apiUrl: "https://example.test/api", timeoutSeconds: 0 } } },
      { research: { llm: { routeId: "writer", baseUrl: "https://example.test/v1?token=secret", model: "model" }, source: { apiUrl: "https://example.test/api", timeoutSeconds: 30 } } }
    ]
    expect(invalid.map(candidate => {
      try { validateConfigCandidate(candidate); return "accepted" } catch (error) { return (error as Error).message }
    })).toEqual([
      "desktop_config_candidate_invalid",
      "desktop_config_candidate_invalid",
      "api_url_invalid",
      "api_url_invalid",
      "desktop_config_source_invalid",
      "base_url_invalid"
    ])
  })
})


