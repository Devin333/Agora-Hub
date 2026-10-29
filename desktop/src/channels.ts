/** Invoke channels of the remote workspace page. */
export const APP_CHANNELS = {
  lifecycleUpdate: "agora:lifecycle:update",
  lifecycleRespond: "agora:lifecycle:respond",
  exportSave: "agora:export:save",
  appearanceSet: "agora:appearance:set"
} as const

/** Invoke channels of the packaged connection page. */
export const CONNECTION_CHANNELS = {
  status: "agora:connection:status",
  retry: "agora:connection:retry",
  choose: "agora:connection:choose",
  edit: "agora:connection:edit",
  cancelEdit: "agora:connection:cancel-edit",
  localCreateAdmin: "agora:connection:local-create-admin",
  localCancel: "agora:connection:local-cancel"
} as const

/** Messages the host sends to a page; never handled as invokes. */
export const HOST_EVENTS = {
  lifecycleQuitRequested: "agora:lifecycle:quit-requested",
  connectionChanged: "agora:connection:changed"
} as const
