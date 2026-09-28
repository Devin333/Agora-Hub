# Execution provider capability admission evidence

Date: 2026-09-28. Scope: task 2.1, local Docker provider and the shared admission boundary. This record is not deployment or rollback qualification for task 2.6.

## Boundaries and behavior

- `ExecutionEnvironmentRegistry` rejects a request before provider invocation when the declared roots, environment isolation, network mode, initial argv policy, child-process policy, memory/CPU/process limits, timeout, cancellation, or termination confirmation exceeds the provider's advertised capabilities. Diagnostics carry typed capability denials; there is no host-process fallback.
- The nonpersistent `ExecutionCancellationSignal` comes from the trusted Harness attempt context through `ToolExecutor`. It stays outside the versioned `ExecutionRequest`. The Research parser adapter goes through the same registry admission path.
- `DockerExecutionEnvironment` advertises `docker-v3`. It supplies isolated mounts, environment, network denial, argv admission, memory and PID limits, timeout and active cancellation with confirmed stop and cleanup. It explicitly rejects network allowlists, child-executable allowlists, CPU-time limits, and unsupported secret injection before launch.
- A cancelled or timed-out attempt quarantines output. A Docker launch/wait/stop or cleanup ambiguity yields an `INDETERMINATE` receipt without interpreting a local CLI kill as proof of container termination.

## Local qualification snapshot

- Code under test: baseline `d1de3c5e` plus the 17 modified tracked `framework/`, `infrastructure/`, and `tests/` files; `git diff --binary -- framework infrastructure tests` SHA-256: `sha256:0fe75cc2c2ed55cbb0028de38be01c773b7ac9b86d9d4e104aae8eede16ea924`. The eventual commit provides the stable merged identity.
- Docker client/server: `29.4.2`/`29.4.2` (local daemon).
- Provider profile: `docker-v3`, capability checksum `sha256:e0faa1f717f8a42cbcccc250e10513fcab71bb2ccf8b02f35b71ca657e604bf0` while the daemon was available.
- Test image: local `redis:7-alpine`, image ID `sha256:9de71018be8413f419688ac34e19dbac432d1c6d9593d2ded99a82eebefeb18f`.
- Focused admission/provider/adapter tests: 83 passed, 2 skipped. Opt-in tests with `NEWSROOM_DOCKER_INTEGRATION=1`: 16 passed against the real Docker daemon. They cover filesystem and environment isolation, denied networking, process and resource limits, active cancellation, timeout, cleanup, and unsupported capability rejection. No `newsroom-exec-*` test containers remained after the run.
- `python -m scripts.dev compile`, strict change validation, and scoped lint passed in the worker slice.
- Integrating pre-commit `python -m scripts.dev smoke`: 3744 passed, 23 deselected; offline AgentLoop run succeeded and `sources validate` reported `is_valid=true`, zero errors/warnings.

This snapshot proves local provider admission for task 2.1. It does not prove production deployment image provenance, all-provider deployment evidence, or active-group rollback under a pinned policy; those remain open under tasks 2.6 and 7.6.
