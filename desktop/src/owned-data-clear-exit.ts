export interface OwnedDataClearSessionBoundary {
  closeAllConnections(): Promise<void>
  flushStorageData(): void
  cookies: { flushStore(): Promise<void> }
}

export type LaunchOwnedDataClearFinalizer = () => Promise<void>

/**
 * Quiesce browser writers before the fixed finalizer is armed. This deliberately
 * does not clear cache or storage: the after-exit finalizer is the single profile
 * deleter and its durable journal is the only completion proof.
 */
export async function armOwnedDataClearFinalizer(
  ownedSession: OwnedDataClearSessionBoundary,
  launchFinalizer: LaunchOwnedDataClearFinalizer
): Promise<void> {
  await ownedSession.closeAllConnections()
  ownedSession.flushStorageData()
  await ownedSession.cookies.flushStore()
  await launchFinalizer()
}
