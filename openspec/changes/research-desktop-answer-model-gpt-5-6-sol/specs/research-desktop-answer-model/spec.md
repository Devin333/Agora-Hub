## ADDED Requirements

### Requirement: Research repo/dev route and Electron owner are distinct
repo/dev Research configuration MAY resolve the dedicated `gpt-5.6-sol` deployment through `NEWS_RESEARCH_LLM_ROUTE_ID=research-desktop-answer`. This environment route MUST NOT be represented as applying to the Electron desktop app. Electron's persisted configuration owner MUST be changed through the desktop config preview/apply workflow and the established release process; the change MUST preserve its fixed credential reference. Secrets MUST NOT be committed or returned in diagnostics. The dedicated route MUST NOT change generic writer, evaluation answer-generation, or judge routes.

#### Scenario: Repo/dev Research route is configured
- **WHEN** repo/dev Research composition resolves `NEWS_RESEARCH_LLM_ROUTE_ID=research-desktop-answer`
- **THEN** its production client MUST use model `gpt-5.6-sol` and the configured Research base URL
- **AND** its credential MUST be resolved only from the configured credential environment-variable name
- **AND** this MUST NOT be treated as evidence that Electron desktop configuration was applied

#### Scenario: Electron preview succeeds but release gate rejects apply
- **WHEN** public config preview accepts model `gpt-5.6-sol` and base URL `https://deedee.tech/v1`
- **AND** config apply returns `desktop_runtime_config_release_required`
- **THEN** the persisted owner MUST remain unchanged
- **AND** the failed apply MUST NOT be represented as an actual desktop switch
- **AND** the release validation root cause MUST be fixed rather than bypassed

#### Scenario: Inherited repo model configuration cannot pollute admission
- **WHEN** the parent process inherits a repo `NEWS_MODELS_CONFIG` value
- **THEN** desktop config admission MUST evaluate the active release inside the desktop owned environment
- **AND** admission MUST occur before stopping the owned runtime
- **AND** an invalid confirmation token or failed release gate MUST NOT stop the runtime or write configuration

#### Scenario: Installed release dependencies drift from the manifest
- **WHEN** active release verification returns `desktop_release_dependency_drift`
- **THEN** config apply MUST remain fail closed
- **AND** the release MUST be rebuilt and reinstalled through the verified release workflow
- **AND** dependencies MUST NOT be patched into the installed environment and the manifest MUST NOT be altered to bypass validation

#### Scenario: Electron model and credential are activated
- **WHEN** the Electron desktop Research model is ready for actual activation
- **THEN** the credential MUST first be provisioned through the controlled credential path
- **AND** the persisted desktop config owner MUST be updated through config preview/apply
- **AND** the fixed credential reference MUST be preserved
- **AND** the established release process MUST NOT be bypassed
- **AND** completion MUST NOT be claimed until the persisted owner and applied runtime model are verified

#### Scenario: Generic writer route remains isolated
- **WHEN** the repo/dev Research model is configured as `gpt-5.6-sol`
- **THEN** the `writer-primary` deployment MUST retain its existing model and provider configuration
- **AND** evaluation judge configuration MUST remain unchanged

#### Scenario: Structured output is not implicitly qualified
- **WHEN** the new deployment has not been admitted through structured-output qualification
- **THEN** its configuration MUST NOT declare native schema or JSON-object fallback support
- **AND** structured-output requests MUST continue to fail closed

### Requirement: Text desktop runtime does not eagerly require optional image dependencies
The text desktop runtime MUST be able to import its actual API and Research composition without Pillow when Marker image processing is not selected. Pillow MUST be loaded at the Marker raster-validation use boundary. This MUST NOT expand the text runtime dependency profile, disable release validation, or bypass raster safety checks.

#### Scenario: Text runtime has no Pillow
- **WHEN** the text runtime imports `interfaces.api.app` and importing `PIL` is unavailable
- **THEN** API import MUST succeed without fabricating a Pillow module
- **AND** selecting actual Marker raster validation without its dependency MUST fail explicitly rather than accept an unvalidated image
- **AND** existing deterministic image validation MUST remain unchanged when Pillow is available
