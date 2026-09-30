# Canonical runtime event wiring evidence

## Scope

This evidence covers the generic `AgentLoopGraphRuntimeComposition` path for
task 3.1. The composition now requires a durable `HarnessTransitionPort` to
expose a `CanonicalRuntimeEventPublisher`. `DurableHarnessEventPort` constructs
that publisher from its own canonical `EventRuntime` and its adapter tenant
scope, so Graph control-plane events and AgentLoop runtime facts share one
durable owner.

Before Graph dispatch starts, the composition binds the publisher to
`AgentRunner`. The runner passes the same sink to the AgentLoop recorder and
the ToolExecutor, and refuses a callback-only or non-durable sink when the
production binding method is used. The publisher exposes the selected runtime
and tenant scope for composition and test inspection without creating a second
event store.

## Verification

The focused integration test
`tests/interfaces/services/test_agent_loop_graph_service.py::test_runtime_composition_installs_agent_loop_into_the_graph_dispatcher`
executes a real Graph-bound AgentLoop with a SQLite `EventRuntime`, then reads
the canonical stream by the exact Graph `run_id`. It verifies that AgentLoop
facts are durable, tenant-scoped, checksummed, and owned by the same publisher
selected by the composition. The companion test verifies that a durable port
without the canonical publisher is rejected before Graph execution.

The targeted service and runtime projection suites pass after this change.
The evidence is limited to the generic Graph-bound AgentLoop composition; it
does not claim complete coverage for every worker producer, parent continuation
path, or the separately gated Research dynamic composition. The OpenSpec task
checkbox remains open until those broader boundaries have qualifying evidence.
