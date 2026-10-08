# Runtime Production Composition Evidence

## Evidence Status

This ledger qualifies only the execution-wiring slice defined by `prd.md`.

| Dimension | Status | Meaning |
| --- | --- | --- |
| PRD implementation | implementation-complete | process-local composition, ToolExecutor injection, selected Research parser adapter, fail-closed admission, caller inventory, and local gates are implemented |
| Docker isolation | environment-blocked | the authoring host has no available Docker daemon; no live parser process receipt is claimed |
| Child/event/recovery/MCP follow-ups | out-of-scope handoff | these are not completion requirements for this change and are not represented as execution-composition ports |
| Production release | not claimed | local contract and smoke evidence does not constitute a target-deployment release signature |

An evidence row is honest only when it records the exact commit, command, environment, result class, and relevant receipt/reference. Result classes are `contract-pass`, `integration-pass`, `blocked`, `skip`, and `qualification-complete`.

## Baseline Snapshot

| Field | Value |
| --- | --- |
| Baseline date | 2026-08-26 |
| Baseline commit | `6bbf3eed` |
| `harness-runtime-execution-safety` | 23/28 at baseline; reused execution contracts, pending predecessor qualification not claimed here |
| `durable-event-runtime` | 53/55 at baseline; remains its own owner |
| `harness-graph` | Canonical Graph authority; the historical 99/100 baseline was reported by the retired Workflow-era proposal |
| `source-policy-contract-convergence` | 41/41 complete at baseline |
| Docker daemon on authoring host | unavailable |

## Implementation Snapshot

| Field | Value |
| --- | --- |
| Snapshot date | 2026-08-26 |
| Key implementation commits | `c5b9f44b`, `54e2ee82`, `a268a0df`, `f2846ccc`, `6bbf3eed`, `9cf2a46d`, `9fbf17fe`, `770e5e25`, `bf9a0e9a` |
| Composition identity | `newsroom-runtime`, version `1` |
| Composition fingerprint | `sha256:547e264e216ea3e8b98c8d133b8d03446328d7191c91534b761c174782d9706e` |
| Policy fingerprint | `sha256:3263923c9ddb6fc67d29ec2dcb2046151cf5937e537b61180316abd14ca3580d` |
| Provider fingerprint | `sha256:ec5b242b5643d44470cff3dff6f4851bf173537c1f54926adf871b56aaa4a3e5` |
| Profiles | `runtime-trusted-in-process`; `research-parser-marker`; `research-parser-mineru` |
| Provider | `docker`, registered with `available=false` on the authoring host |
| Default process role | `ready`; Docker is catalogued but not a startup dependency until an external activity is selected |
| Research parser role | `blocked`; `required_providers=[docker]`, `unavailable_providers=[docker]` |
| Production vertical slice | Research parser -> `ResearchParserExecutionAdapter` -> `ExecutionEnvironmentRegistry` -> Docker provider |

`RuntimeCompositionManifest` is a process-local versioned value object. It fingerprints execution config/policy/provider identity; it is not a distributed manifest service and does not contain child, durable-event, projection, operator, approval, or business side-effect ports.

## Scope Ownership and Handoff

| Boundary | Owner / follow-up | Evidence in this change |
| --- | --- | --- |
| execution registry, profiles, provider admission, ToolExecutor factory | `harness-runtime-production-composition` | implemented and locally qualified |
| selected Research Marker/MinerU parser | this change | controlled adapter implemented; Docker live execution blocked by environment |
| PDF compiler / Nougat | future parser/compiler vertical slice | no host-process fallback; caller inventory status remains `blocked` |
| outbound MCP/sidecar execution | future dedicated change | no production profile enabled; unsupported network/credential capabilities fail closed |
| child dispatch, lease, heartbeat, restart | `harness-child-supervisor-integration` | existing contracts reused; no completion claim |
| durable event, projection cursor, reconnect | `durable-event-runtime`, `runtime-event-operator-wiring` | existing owners unchanged; no execution-composition port binding |
| side-effect intent/outbox/reconciliation | `runtime-recovery-qualification` | execution receipt is not a business side-effect receipt |
| approval, artifact publication, memory write, routing, quality verdict | existing Harness/application owners | authority unchanged and covered by architecture/smoke regression |

## Capability and Profile Matrix

| Capability/profile | Declared behavior | Result |
| --- | --- | --- |
| `runtime-trusted-in-process` | explicit deterministic pure functions only | contract-pass |
| `sandboxed_process` | framework mode exists; no production profile/provider enabled by this slice | admission-gated |
| `research-parser-marker` | `external_process`, provider `docker`, argv prefix `marker_single` | contract-pass / environment-blocked |
| `research-parser-mineru` | `external_process`, provider `docker`, argv prefix `mineru` | contract-pass / environment-blocked |
| filesystem roots and mount mapping | canonical read/write roots; symlink/traversal rejection | contract-pass |
| environment policy | protected variables rejected; parser variables explicitly allowlisted | contract-pass |
| network policy | selected parser requests network deny | contract-pass; no live isolation receipt |
| timeout/cancellation | timeout mapped; unconfirmed termination produces `INDETERMINATE` | contract-pass |
| Docker unavailable | stable `execution_provider_unavailable` denial; no host fallback | blocked as expected |
| Docker unsupported capabilities | network allowlist, secret handle, CPU/generic resource, child-executable allowlist denied with `newsroom.execution-capability-denials/v1` codes | contract-pass |

## Acceptance Criteria Matrix

| AC | Result | Evidence |
| --- | --- | --- |
| AC-01 composition injection | contract-pass | runner, Harness tool-result, batch, external subagent, API/worker/CLI, and Research composition tests in the focused command below |
| AC-02 fail-closed admission | contract-pass | execution-environment, ToolRuntime, API readiness, and diagnostics negative tests |
| AC-03 controlled Research parser | contract-pass | adapter tests plus compiler/cascade/default-composition tests prove exact identity propagation and forbid PyMuPDF/abstract fallback after an execution denial |
| AC-04 roots/env/network/timeout/termination receipt | contract-pass | execution-environment contract/adversarial tests plus parser adapter mapping tests |
| AC-05 Docker unavailable honesty | blocked as expected | default role `ready`; Research role `blocked`; default parser composition returns `execution_environment_unavailable`, with no provider execution, host fallback, or live-isolation claim |
| AC-06 explicit trusted profile | contract-pass | profile catalog and unsafe-downgrade admission tests |
| AC-07 control-plane authority unchanged | contract-pass | legacy control-plane bundle removed; architecture and smoke regressions pass |
| AC-08 reproducible gates | contract-pass | focused, compile, smoke, source validation, and strict OpenSpec rows below |

## Test and Deployment Ledger

| Date | Commit | Environment | Command | Result class | Result | Receipt/reference | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-26 | `770e5e25`, `bf9a0e9a` | local `.venv` | `.\.venv\Scripts\python.exe -m pytest tests/framework/agent/loop tests/framework/agent/subagents tests/framework/harness/runtime/test_tool_result_adapter.py tests/framework/tool/runtime tests/framework/execution_environment tests/interfaces/services/test_agent_loop_graph_service.py tests/interfaces/cli/test_cli_worker_commands.py tests/interfaces/api/test_runtime_execution_composition_api.py tests/interfaces/services/test_diagnostic_application_service.py tests/interfaces/composition/test_research_entrypoint_defaults.py tests/interfaces/composition/test_research_composition.py tests/infrastructure/research/test_document_execution_adapter.py tests/infrastructure/research/test_source_adapters.py tests/backend/research/document/test_cascade_parser.py tests/backend/research/document/test_source_format_and_pdf.py tests/architecture/test_production_caller_inventory.py -q` | contract-pass | 277 passed, 2 skipped | test assertions only | 24 FastAPI lifecycle deprecation warnings |
| 2026-08-26 | `770e5e25`, `bf9a0e9a` | local `.venv` | `.\.venv\Scripts\python.exe -m scripts.dev compile` | contract-pass | compileall passed for `business framework infrastructure interfaces scripts` | - | no network |
| 2026-08-26 | `770e5e25`, `bf9a0e9a` | local `.venv` | `.\.venv\Scripts\python.exe -m scripts.dev smoke` | contract-pass | 2345 passed, 23 deselected; compile, Harness/Research/API/service/architecture, AgentLoop CLI, and source validation passed | `sha256:39ca918cb7c42214ae0b6f1e562a3bacf3705773402f5e9c26623ee433e84d8b` | verified pre-commit tree was committed unchanged; 22 FastAPI lifecycle deprecation warnings; Docker qualification remains blocked |
| 2026-08-26 | `770e5e25`, `bf9a0e9a` | local | `openspec validate harness-runtime-production-composition --strict`; `openspec validate --all --strict` | contract-pass | target change valid; 538 repository changes/specs passed, 0 failed | - | rerun after final documentation update before commit |

## Independent Review

The final execution-only scope, caller inventory, AC-01 through AC-08, authority handoffs, and parser failure path received an independent read-only review after the P0 repair. The review found no remaining P0 or P1 issue. In particular, it traced the exact `GraphExecutionIdentity` from `ResearchSinglePaperRuntime` through `ResearchDocumentCompilerAdapter` and `CascadeDocumentParser` to `ResearchParserExecutionAdapter`, and confirmed that an `ExecutionEnvironmentError` cannot reach PyMuPDF or abstract fallback.

## Remaining Environment Blocker

The only qualification blocker inside this PRD's selected production profile is the unavailable Docker daemon. A target environment with a reachable daemon must execute the selected parser and attach a real `ExecutionReceipt` before Docker isolation can be labeled `qualification-complete`. Until then:

- Research parser readiness/admission remains typed blocked.
- Local fakes and contract tests prove mapping and fail-closed behavior only.
- No host process fallback is permitted.
- Child/event/recovery/MCP follow-ups remain separate work, not hidden blockers for this execution-only change.
