export interface SingleInstanceApplication {
  requestSingleInstanceLock(): boolean
}

export function acquireSingleInstanceLock(application: SingleInstanceApplication): boolean {
  return application.requestSingleInstanceLock()
}
