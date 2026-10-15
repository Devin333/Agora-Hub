export const IPC_CHANNELS = {
  runtimeStatus: "agora:runtime:status",
  runtimeRetry: "agora:runtime:retry",
  runtimeDiagnostics: "agora:runtime:diagnostics",
  runtimeBackup: "agora:runtime:backup",
  runtimeRestorePreview: "agora:runtime:restore:preview",
  runtimeRestoreActivate: "agora:runtime:restore:activate",
  runtimeClearDataPreview: "agora:runtime:clear-data:preview",
  runtimeClearDataApply: "agora:runtime:clear-data:apply",
  runtimeClearDataStatus: "agora:runtime:clear-data:status",
  configShow: "agora:config:show",
  configPreview: "agora:config:preview",
  configApply: "agora:config:apply",
  credentialStatus: "agora:credential:status",
  credentialReplace: "agora:credential:replace",
  credentialRemove: "agora:credential:remove",
  lifecycleUpdate: "agora:lifecycle:update",
  lifecycleRespond: "agora:lifecycle:respond",
  lifecycleQuitRequested: "agora:lifecycle:quit-requested",
  exportSave: "agora:export:save"
} as const


