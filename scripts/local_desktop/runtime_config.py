from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from framework.llm.clients.config import (
    LLMConfigurationError,
    load_openai_compatible_deployment,
    validate_openai_compatible_models_config,
)
from interfaces.composition.research_settings import (
    DESKTOP_RESEARCH_LLM_ENABLED_ENV,
    ResearchLLMSettings,
    ResearchSourceSettings,
)
from scripts.local_desktop.common import (
    DesktopOperationError,
    OperationResult,
    atomic_write_json,
    exclusive_lock,
    protect_private_path,
    read_json_object,
    reject_reparse_path,
)

RUNTIME_CONFIG_SCHEMA = "newsroom.desktop-runtime-settings/v1"
PREVIEW_SCHEMA = "newsroom.desktop-runtime-config-preview/v1"
JOURNAL_SCHEMA = "newsroom.desktop-runtime-config-journal/v1"
FIXED_PROVIDER = "openai-compatible"
FIXED_CREDENTIAL_REF = "paper-agent/research/openai-compatible"
FIXED_CREDENTIAL_ENV = "NEWSROOM_DESKTOP_RESEARCH_API_KEY"
DEFAULT_ROUTE_ID = "writer-primary"
DEFAULT_ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_ARXIV_TIMEOUT_SECONDS = 90.0
_MAX_CANDIDATE_BYTES = 16 * 1024
_PREVIEW_TTL_SECONDS = 15 * 60
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,127}$")
_SECRET_KEY_PARTS = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token", "credential_value")
_SECRET_VALUE = re.compile(r"(?i)(?:^|\s)(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+\S+)")
_SECRET_URL_PART = re.compile(r"(?i)(?:api[-_]?key|access[-_]?token|auth(?:orization)?|bearer|password|secret|token)")


class DesktopCredentialProvider(Protocol):
    def configured(self, credential_ref: str) -> bool: ...

    def project_environment(self, credential_ref: str, *, environment_name: str) -> Mapping[str, str]: ...


class RuntimeConfigurationReleaseOwner(Protocol):
    def active(self) -> tuple[Path, dict[str, Any]]: ...

    def activate(self, release: str | Path) -> dict[str, Any]: ...

    def rebind_runtime_configuration(
        self,
        release: str | Path,
        *,
        runtime_config_document: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class RuntimeConfigPaths:
    document: Path
    models: Path
    preview_root: Path
    journal: Path
    lock: Path


def runtime_config_paths(config: Any) -> RuntimeConfigPaths:
    return runtime_config_paths_for_data_root(Path(config.runtime_root))


def runtime_config_paths_for_data_root(data_root: Path) -> RuntimeConfigPaths:
    private_root = data_root / "desktop" / "config"
    return RuntimeConfigPaths(
        document=private_root / "research-runtime.json",
        models=private_root / "models.json",
        preview_root=private_root / "previews",
        journal=private_root / "transaction.json",
        lock=private_root / "transaction.lock",
    )


def read_runtime_config_document(path: str | Path) -> dict[str, Any]:
    selected = Path(path)
    reject_reparse_path(selected, root=selected.parent)
    if not selected.exists():
        return _default_document()
    payload = read_json_object(selected)
    return validate_runtime_config_document(payload)



def recover_runtime_config_document(paths: RuntimeConfigPaths) -> dict[str, Any]:
    """Read settings and repair only their deterministic models projection.

    Config resolution shares the CAS lock with preview/apply/recovery. It creates
    nothing for a true first run, never consumes the transaction journal, and
    preserves a candidate models file while a journal still needs recovery.
    """
    if not paths.document.exists() and not paths.journal.exists():
        return _default_document()
    paths.document.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(paths.lock):
        document = read_runtime_config_document(paths.document)
        models = models_config_payload(document)
        reject_reparse_path(paths.models, root=paths.models.parent)
        journal_pending = paths.journal.exists()
        if journal_pending:
            return document
        if models is None:
            paths.models.unlink(missing_ok=True)
            return document
        current_models: Mapping[str, Any] | None = None
        if paths.models.is_file():
            try:
                current_models = read_json_object(paths.models)
            except DesktopOperationError:
                current_models = None
        if current_models != models:
            atomic_write_json(paths.models, models)
            protect_private_path(paths.models)
        return document

def validate_runtime_config_document(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise DesktopOperationError("desktop_runtime_settings_invalid", "desktop runtime settings must be an object")
    unknown = sorted(set(payload) - {"schema", "revision", "research"})
    if unknown:
        raise DesktopOperationError("desktop_runtime_settings_unknown_field", f"desktop runtime settings contain unknown fields: {', '.join(unknown)}")
    if payload.get("schema") != RUNTIME_CONFIG_SCHEMA:
        raise DesktopOperationError("desktop_runtime_settings_schema_unsupported", "desktop runtime settings schema is unsupported")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DesktopOperationError("desktop_runtime_settings_revision_invalid", "desktop runtime settings revision must be a non-negative integer")
    research = payload.get("research")
    if not isinstance(research, Mapping):
        raise DesktopOperationError("desktop_runtime_settings_invalid", "desktop runtime settings research section is invalid")
    unknown_research = sorted(set(research) - {"llm", "source"})
    if unknown_research:
        raise DesktopOperationError("desktop_runtime_settings_unknown_field", f"research settings contain unknown fields: {', '.join(unknown_research)}")
    llm = research.get("llm")
    source = research.get("source")
    normalized_llm = _validate_llm(llm) if llm is not None else None
    normalized_source = _validate_source(source if source is not None else {})
    normalized = {
        "schema": RUNTIME_CONFIG_SCHEMA,
        "revision": revision,
        "research": {"llm": normalized_llm, "source": normalized_source},
    }
    _reject_secret_material(normalized)
    if normalized_llm is not None:
        _validate_composition(normalized)
    return normalized


def parse_candidate_json(value: str) -> dict[str, Any]:
    if len(value.encode("utf-8")) > _MAX_CANDIDATE_BYTES:
        raise DesktopOperationError("desktop_runtime_candidate_too_large", "desktop runtime configuration candidate exceeds its size limit")
    try:
        payload = json.loads(value)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError("desktop_runtime_candidate_invalid", "desktop runtime configuration candidate is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DesktopOperationError("desktop_runtime_candidate_invalid", "desktop runtime configuration candidate must be an object")
    return payload


def candidate_document(candidate: Mapping[str, Any], *, revision: int) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise DesktopOperationError("desktop_runtime_candidate_invalid", "desktop runtime configuration candidate must be an object")
    unknown = sorted(set(candidate) - {"llm", "source"})
    if unknown:
        raise DesktopOperationError("desktop_runtime_candidate_unknown_field", f"configuration candidate contains unknown fields: {', '.join(unknown)}")
    return validate_runtime_config_document(
        {
            "schema": RUNTIME_CONFIG_SCHEMA,
            "revision": revision,
            "research": {"llm": candidate.get("llm"), "source": candidate.get("source", {})},
        }
    )


def models_config_payload(document: Mapping[str, Any]) -> dict[str, Any] | None:
    return _models_payload_from_validated(validate_runtime_config_document(document))


def _models_payload_from_validated(validated: Mapping[str, Any]) -> dict[str, Any] | None:
    llm = validated["research"]["llm"]
    if llm is None:
        return None
    deployment_id = "desktop-research-primary"
    return {
        "schema_version": 1,
        "default_route_id": llm["route_id"],
        "routes": {llm["route_id"]: {"deployment_id": deployment_id}},
        "deployments": [
            {
                "deployment_id": deployment_id,
                "provider": FIXED_PROVIDER,
                "model": llm["model"],
                "api_base": llm["base_url"],
                "api_key_env": FIXED_CREDENTIAL_ENV,
            }
        ],
    }


def runtime_environment_overlay(document: Mapping[str, Any], *, models_path: Path | None = None) -> dict[str, str]:
    return _runtime_environment_overlay_validated(validate_runtime_config_document(document), models_path=models_path)


def _runtime_environment_overlay_validated(validated: Mapping[str, Any], *, models_path: Path | None = None) -> dict[str, str]:
    source = validated["research"]["source"]
    values = {
        DESKTOP_RESEARCH_LLM_ENABLED_ENV: "1" if validated["research"]["llm"] is not None else "0",
        "NEWS_RESEARCH_SOURCE_PROVIDER": "arxiv",
        "NEWS_RESEARCH_ARXIV_API_URL": source["api_url"],
        "NEWS_RESEARCH_SOURCE_TIMEOUT_SECONDS": _number_text(source["timeout_seconds"]),
    }
    llm = validated["research"]["llm"]
    if llm is not None:
        if models_path is None:
            raise DesktopOperationError("desktop_runtime_models_path_missing", "configured desktop runtime requires an owned models path")
        values.update(
            {
                "NEWS_MODELS_CONFIG": str(models_path),
                "NEWS_RESEARCH_MODELS_CONFIG": str(models_path),
                "NEWS_RESEARCH_LLM_ROUTE_ID": llm["route_id"],
                "NEWS_RESEARCH_LLM_PROVIDER": FIXED_PROVIDER,
                "NEWS_RESEARCH_LLM_MODEL": llm["model"],
                "NEWS_RESEARCH_LLM_BASE_URL": llm["base_url"],
                "NEWS_RESEARCH_LLM_API_KEY_ENV": FIXED_CREDENTIAL_ENV,
            }
        )
    return values


def runtime_config_digest(document: Mapping[str, Any]) -> str:
    return _json_digest(validate_runtime_config_document(document))


class RuntimeConfigManager:
    def __init__(
        self,
        config: Any,
        *,
        release_manager: RuntimeConfigurationReleaseOwner,
        credential_provider: DesktopCredentialProvider | None = None,
    ) -> None:
        self.config = config
        self.release_manager = release_manager
        self.credential_provider = credential_provider
        self.paths = runtime_config_paths(config.desktop if hasattr(config, "desktop") else config)

    def show(self) -> OperationResult:
        self._ensure_private_roots()
        with exclusive_lock(self.paths.lock):
            document = read_runtime_config_document(self.paths.document)
            projection = _public_projection(document, credential_configured=self._credential_configured(document))
        return OperationResult(
            True,
            "config-show",
            "CONFIGURED" if document["research"]["llm"] is not None else "UNCONFIGURED",
            "desktop_runtime_config_read",
            "desktop runtime configuration was read without exposing credentials",
            {"configuration": projection, "effect_scope": _effect_scope()},
        )

    def preview(self, candidate: Mapping[str, Any]) -> OperationResult:
        candidate_document(candidate, revision=1)
        self._ensure_private_roots()
        with exclusive_lock(self.paths.lock):
            current = read_runtime_config_document(self.paths.document)
            proposed = candidate_document(candidate, revision=current["revision"] + 1)
            digest = runtime_config_digest(proposed)
            nonce = token_hex(16)
            token = f"desktop-config:{nonce}:{digest[:16]}"
            receipt = {
                "schema": PREVIEW_SCHEMA,
                "token": token,
                "base_revision": current["revision"],
                "candidate_digest": digest,
                "candidate": proposed,
                "created_at_epoch": time.time(),
            }
            atomic_write_json(self.paths.preview_root / f"{nonce}.json", receipt)
        return OperationResult(
            True,
            "config-preview",
            "CONFIRMATION_REQUIRED",
            "desktop_runtime_config_preview_ready",
            "desktop runtime configuration candidate is valid and requires explicit confirmation",
            {
                "configuration": _public_projection(proposed, credential_configured=self._credential_configured(proposed)),
                "base_revision": current["revision"],
                "candidate_digest": digest,
                "confirmation_token": token,
                "requires_owned_runtime_stop": True,
                "effect_scope": _effect_scope(),
            },
        )

    def admit(
        self,
        candidate: Mapping[str, Any],
        *,
        expected_revision: int,
        confirmation_token: str,
    ) -> dict[str, Any]:
        """Read-only admission gate used before stopping an owned runtime."""
        self._ensure_private_roots()
        with exclusive_lock(self.paths.lock):
            return self._admit_unlocked(
                candidate,
                expected_revision=expected_revision,
                confirmation_token=confirmation_token,
            )

    def _admit_unlocked(
        self,
        candidate: Mapping[str, Any],
        *,
        expected_revision: int,
        confirmation_token: str,
    ) -> dict[str, Any]:
        current = read_runtime_config_document(self.paths.document)
        if current["revision"] != expected_revision:
            raise DesktopOperationError("desktop_runtime_config_revision_conflict", "desktop runtime configuration changed after preview")
        proposed = candidate_document(candidate, revision=expected_revision + 1)
        receipt_path, receipt = self._read_preview(confirmation_token)
        if receipt.get("base_revision") != expected_revision or receipt.get("candidate_digest") != runtime_config_digest(proposed) or receipt.get("candidate") != proposed:
            raise DesktopOperationError("desktop_runtime_config_confirmation_mismatch", "desktop runtime configuration confirmation does not match the candidate")
        try:
            active_path, active_manifest = self.release_manager.active()
        except DesktopOperationError as exc:
            raise DesktopOperationError("desktop_runtime_config_release_required", "an active verified release is required before applying desktop runtime configuration") from exc
        return {
            "current_document": current,
            "candidate_document": proposed,
            "receipt_path": receipt_path,
            "active_release_path": active_path,
            "active_manifest": active_manifest,
        }

    def apply(
        self,
        candidate: Mapping[str, Any],
        *,
        expected_revision: int,
        confirmation_token: str,
    ) -> OperationResult:
        self._ensure_private_roots()
        with exclusive_lock(self.paths.lock):
            admission = self._admit_unlocked(
                candidate,
                expected_revision=expected_revision,
                confirmation_token=confirmation_token,
            )
            current = admission["current_document"]
            proposed = admission["candidate_document"]
            receipt_path = admission["receipt_path"]
            active_path = Path(admission["active_release_path"])
            active_manifest = admission["active_manifest"]
            rebound = self.release_manager.rebind_runtime_configuration(active_path, runtime_config_document=proposed)
            rebound_path = Path(str(rebound["release_path"]))
            journal = {
                "schema": JOURNAL_SCHEMA,
                "status": "prepared",
                "previous_document": current,
                "previous_release_path": str(active_path),
                "candidate_document": proposed,
                "candidate_release_path": str(rebound_path),
            }
            atomic_write_json(self.paths.journal, journal)
            receipt_path.unlink(missing_ok=True)
            try:
                self._write_document(proposed)
                journal["status"] = "config_written"
                atomic_write_json(self.paths.journal, journal)
                pointer = self.release_manager.activate(rebound_path)
                journal["status"] = "activated"
                atomic_write_json(self.paths.journal, journal)
            except Exception as exc:
                rollback_failures: list[str] = []
                try:
                    self._write_document(current)
                except Exception:
                    rollback_failures.append("configuration")
                try:
                    self.release_manager.activate(active_path)
                except Exception:
                    rollback_failures.append("release")
                if rollback_failures:
                    journal["status"] = "rollback_failed"
                    journal["rollback_failures"] = rollback_failures
                    try:
                        atomic_write_json(self.paths.journal, journal)
                    except Exception:
                        pass
                    raise DesktopOperationError(
                        "desktop_runtime_config_rollback_failed",
                        "desktop runtime configuration failed and automatic rollback did not complete",
                        details={"rollback_failures": rollback_failures},
                    ) from exc
                self.paths.journal.unlink(missing_ok=True)
                raise
            self.paths.journal.unlink(missing_ok=True)
        return OperationResult(
            True,
            "config-apply",
            "APPLIED",
            "desktop_runtime_config_applied",
            "desktop runtime configuration was committed and rebound to a verified immutable release",
            {
                "configuration": _public_projection(proposed, credential_configured=self._credential_configured(proposed)),
                "previous_release_id": active_manifest["identity"]["release_id"],
                "release_id": rebound["manifest"]["identity"]["release_id"],
                "active_release": pointer,
                "effect_scope": _effect_scope(),
            },
        )

    def recover_pending(self) -> dict[str, Any] | None:
        self._ensure_private_roots()
        with exclusive_lock(self.paths.lock):
            reject_reparse_path(self.paths.journal, root=self.paths.journal.parent)
            if not self.paths.journal.exists():
                return None
            journal = read_json_object(self.paths.journal)
            if journal.get("schema") != JOURNAL_SCHEMA:
                raise DesktopOperationError("desktop_runtime_config_journal_invalid", "desktop runtime configuration journal is invalid")
            status = journal.get("status")
            if status not in {"prepared", "config_written", "activated", "rollback_failed"}:
                raise DesktopOperationError("desktop_runtime_config_journal_invalid", "desktop runtime configuration journal status is invalid")
            if status in {"prepared", "rollback_failed"}:
                previous = validate_runtime_config_document(journal["previous_document"])
                previous_release = str(journal["previous_release_path"])
                try:
                    self._write_document(previous)
                    pointer = self.release_manager.activate(previous_release)
                except Exception as exc:
                    raise DesktopOperationError(
                        "desktop_runtime_config_recovery_failed",
                        "desktop runtime configuration rollback recovery did not complete",
                    ) from exc
                self.paths.journal.unlink(missing_ok=True)
                return {"status": "rolled_back", "active_release": pointer}
            candidate = validate_runtime_config_document(journal["candidate_document"])
            try:
                self._write_document(candidate)
                pointer = self.release_manager.activate(str(journal["candidate_release_path"]))
            except Exception as exc:
                raise DesktopOperationError(
                    "desktop_runtime_config_recovery_failed",
                    "desktop runtime configuration commit recovery did not complete",
                ) from exc
            self.paths.journal.unlink(missing_ok=True)
            return {"status": "completed", "active_release": pointer}

    def _credential_configured(self, document: Mapping[str, Any]) -> bool | None:
        llm = document["research"]["llm"]
        if llm is None or self.credential_provider is None:
            return None
        return bool(self.credential_provider.configured(FIXED_CREDENTIAL_REF))

    def _read_preview(self, token: str) -> tuple[Path, dict[str, Any]]:
        parts = str(token).split(":")
        if len(parts) != 3 or parts[0] != "desktop-config" or not re.fullmatch(r"[0-9a-f]{32}", parts[1]) or not re.fullmatch(r"[0-9a-f]{16}", parts[2]):
            raise DesktopOperationError("desktop_runtime_config_confirmation_invalid", "desktop runtime configuration confirmation token is invalid")
        receipt_path = self.paths.preview_root / f"{parts[1]}.json"
        reject_reparse_path(receipt_path, root=self.paths.preview_root)
        if not receipt_path.is_file():
            raise DesktopOperationError("desktop_runtime_config_confirmation_expired", "desktop runtime configuration confirmation is missing or expired")
        receipt = read_json_object(receipt_path)
        if receipt.get("schema") != PREVIEW_SCHEMA or receipt.get("token") != token:
            raise DesktopOperationError("desktop_runtime_config_confirmation_invalid", "desktop runtime configuration confirmation receipt is invalid")
        created_at = receipt.get("created_at_epoch")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise DesktopOperationError("desktop_runtime_config_confirmation_invalid", "desktop runtime configuration confirmation receipt timestamp is invalid")
        age = time.time() - float(created_at)
        if age < -60 or age > _PREVIEW_TTL_SECONDS:
            raise DesktopOperationError("desktop_runtime_config_confirmation_expired", "desktop runtime configuration confirmation is missing or expired")
        return receipt_path, receipt

    def _ensure_private_roots(self) -> None:
        owned_root = Path(self.config.data_root if hasattr(self.config, "data_root") else self.config.runtime_root)
        directories = (self.paths.document.parent, self.paths.models.parent, self.paths.preview_root)
        protected: set[Path] = set()
        for path in (*directories, self.paths.document, self.paths.models, self.paths.journal, self.paths.lock):
            reject_reparse_path(path, root=owned_root)
        for directory in directories:
            relative = directory.relative_to(owned_root)
            current = owned_root
            for part in ("", *relative.parts):
                if part:
                    current = current / part
                reject_reparse_path(current, root=owned_root)
                current.mkdir(exist_ok=True)
                reject_reparse_path(current, root=owned_root)
                if current not in protected:
                    protect_private_path(current)
                    protected.add(current)

    def _write_document(self, document: Mapping[str, Any]) -> None:
        validated = validate_runtime_config_document(document)
        self._ensure_private_roots()
        models = models_config_payload(validated)
        if models is None:
            self.paths.models.unlink(missing_ok=True)
        else:
            atomic_write_json(self.paths.models, models)
        atomic_write_json(self.paths.document, validated)


def _validate_llm(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesktopOperationError("desktop_runtime_llm_invalid", "research LLM configuration must be an object")
    allowed = {"route_id", "provider", "model", "base_url", "credential_ref", "credential_env"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DesktopOperationError("desktop_runtime_candidate_unknown_field", f"research LLM configuration contains unknown fields: {', '.join(unknown)}")
    route_id = str(value.get("route_id") or DEFAULT_ROUTE_ID).strip()
    provider = str(value.get("provider") or FIXED_PROVIDER).strip()
    model = str(value.get("model") or "").strip()
    base_url = str(value.get("base_url") or "").strip().rstrip("/")
    credential_ref = str(value.get("credential_ref") or FIXED_CREDENTIAL_REF).strip()
    credential_env = str(value.get("credential_env") or FIXED_CREDENTIAL_ENV).strip()
    if not _IDENTIFIER.fullmatch(route_id):
        raise DesktopOperationError("desktop_runtime_llm_route_invalid", "research LLM route id is invalid")
    if provider != FIXED_PROVIDER:
        raise DesktopOperationError("desktop_runtime_llm_provider_invalid", "desktop Research supports only the openai-compatible provider")
    if not model or len(model) > 256 or any(ord(character) < 32 for character in model):
        raise DesktopOperationError("desktop_runtime_llm_model_invalid", "research LLM model is invalid")
    _validate_url(base_url, field="research LLM base URL", allow_query=False)
    if credential_ref != FIXED_CREDENTIAL_REF or credential_env != FIXED_CREDENTIAL_ENV:
        raise DesktopOperationError("desktop_runtime_credential_reference_invalid", "research credential reference is not an allowed OS vault binding")
    return {
        "route_id": route_id,
        "provider": FIXED_PROVIDER,
        "model": model,
        "base_url": base_url,
        "credential_ref": FIXED_CREDENTIAL_REF,
        "credential_env": FIXED_CREDENTIAL_ENV,
    }


def _validate_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesktopOperationError("desktop_runtime_source_invalid", "Research source configuration must be an object")
    allowed = {"preset", "api_url", "timeout_seconds"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DesktopOperationError("desktop_runtime_candidate_unknown_field", f"Research source configuration contains unknown fields: {', '.join(unknown)}")
    preset = str(value.get("preset") or "arxiv-default").strip()
    if preset != "arxiv-default":
        raise DesktopOperationError("desktop_runtime_source_preset_invalid", "only the existing arxiv-default Research source preset is supported")
    api_url = str(value.get("api_url") or DEFAULT_ARXIV_API_URL).strip()
    _validate_url(api_url, field="Research Arxiv API URL", allow_query=True)
    timeout_value = value.get("timeout_seconds", DEFAULT_ARXIV_TIMEOUT_SECONDS)
    if isinstance(timeout_value, bool):
        raise DesktopOperationError("desktop_runtime_source_timeout_invalid", "Research source timeout must be a number")
    try:
        timeout = float(timeout_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DesktopOperationError("desktop_runtime_source_timeout_invalid", "Research source timeout must be a number") from exc
    if not 1.0 <= timeout <= 300.0:
        raise DesktopOperationError("desktop_runtime_source_timeout_invalid", "Research source timeout must be between 1 and 300 seconds")
    return {"preset": preset, "api_url": api_url, "timeout_seconds": timeout}


def _validate_url(value: str, *, field: str, allow_query: bool) -> None:
    if len(value) > 2048 or any(ord(character) < 32 for character in value):
        raise DesktopOperationError("desktop_runtime_url_invalid", f"{field} is invalid")
    parsed = urlsplit(value)
    secret_bearing = bool(_SECRET_URL_PART.search(parsed.path) or _SECRET_URL_PART.search(parsed.query))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.query and not allow_query)
        or secret_bearing
    ):
        raise DesktopOperationError("desktop_runtime_url_invalid", f"{field} must be a credential-free HTTP(S) URL")


def _validate_composition(document: Mapping[str, Any]) -> None:
    models = _models_payload_from_validated(document)
    assert models is not None
    validate_openai_compatible_models_config(models)
    with tempfile.TemporaryDirectory(prefix="desktop-runtime-config-") as directory:
        path = Path(directory) / "models.json"
        path.write_text(json.dumps(models, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        llm = document["research"]["llm"]
        try:
            deployment = load_openai_compatible_deployment(path, route_id=llm["route_id"], apply_environment_overrides=False)
        except LLMConfigurationError as exc:
            raise DesktopOperationError("desktop_runtime_llm_config_invalid", "research LLM deployment configuration is invalid") from exc
        if deployment.config.model != llm["model"] or deployment.config.base_url.rstrip("/") != llm["base_url"].rstrip("/"):
            raise DesktopOperationError("desktop_runtime_llm_config_mismatch", "research LLM settings do not match the selected deployment")
        llm_settings = ResearchLLMSettings(
            route_id=document["research"]["llm"]["route_id"],
            models_config_path=path.resolve(),
            provider=FIXED_PROVIDER,
            base_url=document["research"]["llm"]["base_url"],
            model=document["research"]["llm"]["model"],
            api_key_env=FIXED_CREDENTIAL_ENV,
            timeout_seconds=90.0,
            max_attempts=2,
            max_input_tokens=8192,
            max_output_tokens=2048,
        )
    source_settings = ResearchSourceSettings(
        provider="arxiv",
        api_url=document["research"]["source"]["api_url"],
        cache_size=128,
        timeout_seconds=document["research"]["source"]["timeout_seconds"],
        metadata_max_bytes=1_000_000,
        package_max_bytes=120_000_000,
    )
    if llm_settings.model != deployment.config.model or llm_settings.base_url.rstrip("/") != deployment.config.base_url.rstrip("/"):
        raise DesktopOperationError("desktop_runtime_llm_config_mismatch", "Research runtime settings do not match the selected deployment")
    if source_settings.provider != "arxiv":
        raise DesktopOperationError("desktop_runtime_source_config_mismatch", "Research source worker settings do not match the selected source configuration")


def _reject_secret_material(value: Any, *, path: str = "configuration") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(part in normalized for part in _SECRET_KEY_PARTS) and normalized not in {"credential_ref", "credential_env"}:
                raise DesktopOperationError("desktop_runtime_secret_rejected", f"secret-bearing field is not allowed: {path}.{key}")
            _reject_secret_material(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise DesktopOperationError("desktop_runtime_secret_rejected", f"secret-like value is not allowed: {path}")


def _default_document() -> dict[str, Any]:
    return {
        "schema": RUNTIME_CONFIG_SCHEMA,
        "revision": 0,
        "research": {"llm": None, "source": _validate_source({})},
    }


def _effect_scope() -> dict[str, list[str]]:
    return {
        "llm_consumers": ["research", "guided-intent"],
        "source_consumers": ["research-arxiv-provider"],
        "not_changed": ["guided-paper-search-public-cache", "guided-project-search"],
    }


def _public_projection(document: Mapping[str, Any], *, credential_configured: bool | None = None) -> dict[str, Any]:
    validated = validate_runtime_config_document(document)
    llm = validated["research"]["llm"]
    public_llm = None
    if llm is not None:
        public_llm = {
            "route_id": llm["route_id"],
            "provider": llm["provider"],
            "model": llm["model"],
            "base_url": llm["base_url"],
            "credential_configured": credential_configured,
            "credential_binding": "windows-credential-manager",
        }
    return {
        "schema": validated["schema"],
        "revision": validated["revision"],
        "research": {"llm": public_llm, "source": validated["research"]["source"]},
    }


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _json_digest(value: Mapping[str, Any]) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_ARXIV_API_URL",
    "DesktopCredentialProvider",
    "FIXED_CREDENTIAL_ENV",
    "FIXED_CREDENTIAL_REF",
    "FIXED_PROVIDER",
    "RUNTIME_CONFIG_SCHEMA",
    "RuntimeConfigManager",
    "candidate_document",
    "models_config_payload",
    "parse_candidate_json",
    "read_runtime_config_document",
    "runtime_config_digest",
    "runtime_config_paths",
    "runtime_config_paths_for_data_root",
    "runtime_environment_overlay",
    "validate_runtime_config_document",
]
