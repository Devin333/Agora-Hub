# Retired Agent Session Database

The shared agent-session runtime has been retired. NewsRoom no longer creates,
opens, reads, imports, migrates, rewrites, archives, or deletes the former
default database at `.newsroom/paper-agent-sessions.sqlite3`.

Any copy left by an earlier release is **orphaned historical data**. NewsRoom
must not automatically access or delete that file during startup, migration,
cleanup, rollback, or Harness transcript initialization. It is not an input to
the current Harness context, memory, subagent transcript, or TaskPlan runtime.

The operator owns the retention decision. After applying local retention,
privacy, backup, and legal requirements, an operator may retain the file,
archive it outside NewsRoom, or remove it through an explicit out-of-band
operation. Restoring the file does not restore the retired runtime.
