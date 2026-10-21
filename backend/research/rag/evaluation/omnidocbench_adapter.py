from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_SCORER_ROOT = Path("F:/benchmarks/OmniDocBench")
MAX_TIMEOUT_SECONDS = 86_400
MAX_WORKERS = 64
MAX_PREDICTION_FILES = 100_000
MAX_PREDICTION_BYTES = 2 * 1024**3
MAX_ANNOTATION_BYTES = 256 * 1024**2
MAX_CONFIG_BYTES = 4 * 1024**2
_MAX_TRAVERSED_ENTRIES = 200_000
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PredictionInputIdentity:
    relative_path_sha256: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _PredictionEntry:
    relative_path: str
    source_path: Path
    identity: PredictionInputIdentity


@dataclass(frozen=True, slots=True)
class OmniDocBenchRunResult:
    status: str
    scorer_commit: str | None
    config_sha256: str | None
    runtime_config_sha256: str | None
    annotation_sha256: str | None
    prediction_manifest: tuple[PredictionInputIdentity, ...]
    prediction_manifest_sha256: str | None
    prediction_file_count: int
    prediction_total_bytes: int
    exit_code: int | None
    runtime_seconds: float
    termination_status: str
    stdout_path: str | None
    stderr_path: str | None
    scorer_result_dir: str | None
    error: str | None
    summary_path: str | None
    staged_input_dir: str | None
    staging_seconds: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_bounded(path: Path, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > max_bytes:
                raise ValueError(f"Input grew beyond its configured byte budget while hashing: {path.name}")
            digest.update(block)
    return digest.hexdigest(), total


def _bounded_positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer from 1 through {maximum}")
    return value


def _resolved_input(path: str | os.PathLike[str], *, label: str, directory: bool | None = None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"{label} path must not be a symbolic link: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{label} path does not exist or cannot be resolved: {candidate}") from exc
    if not (resolved.is_file() or resolved.is_dir()):
        raise ValueError(f"{label} must be a regular file or directory: {resolved}")
    if directory is True and not resolved.is_dir():
        raise ValueError(f"{label} must be a directory: {resolved}")
    if directory is False and not resolved.is_file():
        raise ValueError(f"{label} must be a file: {resolved}")
    return resolved


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _prediction_identities(
    root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
) -> tuple[_PredictionEntry, ...]:
    pending = [root]
    entries_seen = 0
    total_bytes = 0
    identities: list[_PredictionEntry] = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise ValueError(f"Cannot enumerate prediction directory: {directory}") from exc
        for child in children:
            entries_seen += 1
            if entries_seen > _MAX_TRAVERSED_ENTRIES:
                raise ValueError(f"prediction tree exceeds {_MAX_TRAVERSED_ENTRIES} filesystem entries")
            child_path = Path(child.path)
            if child.is_symlink():
                raise ValueError(f"prediction tree must not contain symlinks: {child_path}")
            try:
                resolved = child_path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"Cannot resolve prediction entry: {child_path}") from exc
            if not _inside(resolved, root):
                raise ValueError(f"prediction entry escapes prediction root: {child_path}")
            if child.is_dir(follow_symlinks=False):
                pending.append(resolved)
                continue
            if not child.is_file(follow_symlinks=False) or child_path.suffix.lower() not in {".md", ".mmd"}:
                continue
            if len(identities) >= max_files:
                raise ValueError(f"prediction file count exceeds configured limit {max_files}")
            size = child.stat(follow_symlinks=False).st_size
            total_bytes += size
            if total_bytes > max_total_bytes:
                raise ValueError(f"prediction total size exceeds configured limit {max_total_bytes} bytes")
            relative_path = resolved.relative_to(root).as_posix()
            path_identity = _sha256_bytes(relative_path.encode("utf-8", errors="strict"))
            content_hash, actual_size = _sha256_bounded(resolved, max_total_bytes - (total_bytes - size))
            if actual_size != size:
                raise ValueError(f"Prediction changed while hashing: {child_path.name}")
            identities.append(
                _PredictionEntry(
                    relative_path=relative_path,
                    source_path=resolved,
                    identity=PredictionInputIdentity(
                        relative_path_sha256=path_identity,
                        content_sha256=content_hash,
                        size_bytes=actual_size,
                    ),
                )
            )
    if not identities:
        raise ValueError("prediction directory must contain at least one .md or .mmd prediction")
    return tuple(sorted(identities, key=lambda item: item.relative_path))


def _public_identities(entries: tuple[_PredictionEntry, ...]) -> tuple[PredictionInputIdentity, ...]:
    return tuple(entry.identity for entry in entries)


def _prediction_manifest_sha256(identities: tuple[PredictionInputIdentity, ...]) -> str:
    ordered = sorted(
        identities,
        key=lambda item: (item.relative_path_sha256, item.content_sha256, item.size_bytes),
    )
    canonical = json.dumps(
        [asdict(identity) for identity in ordered], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _copy_verified(
    source: Path,
    target: Path,
    expected: PredictionInputIdentity,
    byte_limit: int,
    *,
    expected_root: Path | None = None,
) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"Staged input source is no longer a regular file: {source}")
    try:
        resolved_source = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("Staged input source cannot be resolved") from exc
    if resolved_source != source or (expected_root is not None and not _inside(resolved_source, expected_root)):
        raise ValueError("Staged input source escaped its admitted root")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as source_stream, target.open("xb") as target_stream:
        while block := source_stream.read(1024 * 1024):
            total += len(block)
            if total > byte_limit:
                raise ValueError("Input changed while staging and exceeded the configured byte budget")
            digest.update(block)
            target_stream.write(block)
    if total != expected.size_bytes or digest.hexdigest() != expected.content_sha256:
        raise ValueError("Input changed between fingerprinting and staging; scorer was not started")
    target.chmod(0o444)


def _stage_inputs(
    output: Path,
    prediction_root: Path,
    annotation: Path,
    entries: tuple[_PredictionEntry, ...],
    annotation_sha256: str,
    *,
    max_prediction_bytes: int,
    max_annotation_bytes: int,
) -> tuple[Path, Path]:
    stage_root = output / "input"
    staged_predictions = stage_root / "predictions"
    staged_predictions.mkdir(parents=True)
    for entry in entries:
        target = staged_predictions / entry.relative_path
        resolved_target = target.resolve(strict=False)
        if not _inside(resolved_target, staged_predictions.resolve(strict=True)):
            raise ValueError("Prediction staging path escapes its isolated input directory")
        _copy_verified(
            entry.source_path, target, entry.identity, entry.identity.size_bytes, expected_root=prediction_root
        )
    staged_annotation = stage_root / "annotations.json"
    annotation_identity = PredictionInputIdentity("", annotation_sha256, annotation.stat().st_size)
    _copy_verified(annotation, staged_annotation, annotation_identity, annotation_identity.size_bytes)
    for directory, _, _ in os.walk(stage_root, topdown=False):
        Path(directory).chmod(0o555)
    return staged_predictions, staged_annotation


def _verify_staged_inputs(
    staged_predictions: Path,
    staged_annotation: Path,
    entries: tuple[_PredictionEntry, ...],
    annotation_sha256: str,
) -> bool:
    try:
        if _sha256(staged_annotation) != annotation_sha256:
            return False
        for entry in entries:
            target = staged_predictions / entry.relative_path
            if target.is_symlink() or target.stat().st_size != entry.identity.size_bytes:
                return False
            if _sha256(target) != entry.identity.content_sha256:
                return False
        return True
    except OSError:
        return False


def _git(scorer_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(scorer_root), *args],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if completed.returncode != 0:
        raise ValueError(f"Cannot inspect OmniDocBench git checkout: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _load_runtime_config(
    config_path: Path,
    prediction_path: Path,
    annotation_path: Path,
    *,
    max_workers: int,
) -> bytes:
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"Scorer config exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load scorer config: {config_path}") from exc
    if not isinstance(config, dict) or set(config) != {"end2end_eval"}:
        raise ValueError("Scorer config must contain only the official end2end_eval task")
    task = config["end2end_eval"]
    try:
        dataset = task["dataset"]
        ground_truth = dataset["ground_truth"]
        prediction = dataset["prediction"]
    except (TypeError, KeyError) as exc:
        raise ValueError("Scorer config is missing the official end2end dataset paths") from exc
    if not isinstance(ground_truth, dict) or not isinstance(prediction, dict):
        raise ValueError("Scorer config dataset path entries must be mappings")
    ground_truth["data_path"] = str(annotation_path)
    prediction["data_path"] = str(prediction_path)
    if "match_workers" in dataset:
        dataset["match_workers"] = min(max_workers, _bounded_positive_int(dataset["match_workers"], "dataset.match_workers", MAX_WORKERS))
    metrics = task.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("Scorer config must declare official metrics")
    for metric_group in metrics.values():
        if isinstance(metric_group, dict):
            for key in ("cdm_workers", "teds_workers"):
                if key in metric_group:
                    metric_group[key] = min(max_workers, _bounded_positive_int(metric_group[key], key, MAX_WORKERS))
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode("utf-8")


def _terminate_windows_process_tree(process: subprocess.Popen[bytes]) -> str:
    system_root = os.environ.get("SystemRoot")
    taskkill = Path(system_root) / "System32" / "taskkill.exe" if system_root else None
    if taskkill is None or not taskkill.is_file() or not isinstance(process.pid, int) or process.pid <= 0:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "indeterminate"
    try:
        result = subprocess.run(
            [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
            shell=False,
            check=False,
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0:
            process.wait(timeout=10)
            return "terminated"
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "indeterminate"


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> str:
    if os.name == "nt":
        return _terminate_windows_process_tree(process)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return "indeterminate"
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if not _posix_process_group_exists(process.pid):
        return "terminated"
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except OSError:
        return "indeterminate"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            process.wait(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        if not _posix_process_group_exists(process.pid):
            return "terminated"
        time.sleep(0.05)
    return "indeterminate"


def _posix_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _write_summary(path: Path, result: OmniDocBenchRunResult) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_omnidocbench_official_scorer(
    *,
    prediction_path: str | os.PathLike[str],
    annotation_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    expected_commit: str,
    expected_config_sha256: str,
    scorer_root: str | os.PathLike[str] = DEFAULT_SCORER_ROOT,
    config_path: str | os.PathLike[str] | None = None,
    timeout_seconds: int = 3600,
    max_workers: int = 4,
    max_prediction_files: int = MAX_PREDICTION_FILES,
    max_prediction_bytes: int = MAX_PREDICTION_BYTES,
    max_annotation_bytes: int = MAX_ANNOTATION_BYTES,
) -> OmniDocBenchRunResult:
    """Run the frozen official scorer into a new, exclusive output directory."""
    if not isinstance(expected_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise ValueError("expected_commit must be a full, lowercase 40-character Git commit SHA")
    if not isinstance(expected_config_sha256, str) or not _SHA256_RE.fullmatch(expected_config_sha256):
        raise ValueError("expected_config_sha256 must be a lowercase 64-character SHA-256 digest")
    timeout_seconds = _bounded_positive_int(timeout_seconds, "timeout_seconds", MAX_TIMEOUT_SECONDS)
    max_workers = _bounded_positive_int(max_workers, "max_workers", MAX_WORKERS)
    max_prediction_files = _bounded_positive_int(max_prediction_files, "max_prediction_files", MAX_PREDICTION_FILES)
    max_prediction_bytes = _bounded_positive_int(max_prediction_bytes, "max_prediction_bytes", MAX_PREDICTION_BYTES)
    max_annotation_bytes = _bounded_positive_int(max_annotation_bytes, "max_annotation_bytes", MAX_ANNOTATION_BYTES)

    root = Path(scorer_root).expanduser().resolve(strict=False)
    if not root.exists():
        return OmniDocBenchRunResult(
            status="not_available", scorer_commit=None, config_sha256=None, runtime_config_sha256=None,
            annotation_sha256=None, prediction_manifest=(), prediction_manifest_sha256=None,
            prediction_file_count=0, prediction_total_bytes=0, exit_code=None, runtime_seconds=0.0,
            termination_status="not_started", stdout_path=None, stderr_path=None, scorer_result_dir=None,
            error=f"OmniDocBench scorer checkout is unavailable: {root}", summary_path=None,
            staged_input_dir=None, staging_seconds=0.0,
        )
    if not root.is_dir():
        raise ValueError(f"scorer_root must be a directory: {root}")

    prediction = _resolved_input(prediction_path, label="prediction", directory=True)
    annotation = _resolved_input(annotation_path, label="annotation", directory=False)
    entries = _prediction_identities(
        prediction, max_files=max_prediction_files, max_total_bytes=max_prediction_bytes
    )
    identities = _public_identities(entries)
    if annotation.stat().st_size > max_annotation_bytes:
        raise ValueError(f"annotation exceeds configured limit {max_annotation_bytes} bytes")
    try:
        with annotation.open("rb") as annotation_stream:
            annotation_bytes = annotation_stream.read(max_annotation_bytes + 1)
        if len(annotation_bytes) > max_annotation_bytes:
            raise ValueError(f"annotation exceeds configured limit {max_annotation_bytes} bytes while reading")
        annotations = json.loads(annotation_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("annotation must be a readable UTF-8 JSON file") from exc
    if not isinstance(annotations, list) or not annotations:
        raise ValueError("annotation JSON must contain a non-empty list of official page annotations")
    annotation_sha256 = _sha256_bytes(annotation_bytes)

    output_candidate = Path(output_path).expanduser()
    if output_candidate.exists() or output_candidate.is_symlink():
        raise FileExistsError(f"output_path must name a new, non-existing directory: {output_candidate}")
    output = output_candidate.resolve(strict=False)
    if not output.parent.is_dir():
        raise ValueError(f"output_path parent directory must already exist: {output.parent}")
    if _inside(output, root) or _inside(root, output):
        raise ValueError("output_path must be outside and disjoint from the scorer checkout")
    if any(_inside(output, item) or _inside(item, output) for item in (prediction, annotation)):
        raise ValueError("output_path must not overlap prediction or annotation paths")

    entrypoint = root / "pdf_validation.py"
    if not entrypoint.is_file():
        return OmniDocBenchRunResult(
            status="not_available", scorer_commit=None, config_sha256=None, runtime_config_sha256=None,
            annotation_sha256=annotation_sha256, prediction_manifest=identities,
            prediction_manifest_sha256=_prediction_manifest_sha256(identities),
            prediction_file_count=len(identities), prediction_total_bytes=sum(item.size_bytes for item in identities),
            exit_code=None, runtime_seconds=0.0, termination_status="not_started", stdout_path=None,
            stderr_path=None, scorer_result_dir=None, error=f"Official scorer entrypoint is unavailable: {entrypoint}",
            summary_path=None, staged_input_dir=None, staging_seconds=0.0,
        )
    entrypoint = entrypoint.resolve(strict=True)
    if not _inside(entrypoint, root):
        raise ValueError("Official scorer entrypoint must resolve inside scorer_root")
    git_root = Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if git_root != root:
        raise ValueError(f"scorer_root is not the root of its Git checkout: {root}")
    actual_commit = _git(root, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise ValueError(f"OmniDocBench commit mismatch: expected {expected_commit}, found {actual_commit}")
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise ValueError(f"OmniDocBench scorer checkout is dirty: {dirty.splitlines()[0]}")

    config = Path(config_path).expanduser().resolve(strict=True) if config_path else root / "configs" / "end2end.yaml"
    if not config.is_file() or not _inside(config, root):
        raise ValueError("config_path must be a regular file inside the frozen scorer checkout")
    actual_config_sha256 = _sha256(config)
    if actual_config_sha256 != expected_config_sha256:
        raise ValueError(
            f"OmniDocBench config fingerprint mismatch: expected {expected_config_sha256}, found {actual_config_sha256}"
        )
    if _inside(output, config) or _inside(config, output):
        raise ValueError("output_path must not overlap the scorer config path")
    # Staging adds bounded I/O but ensures the scorer consumes the exact bytes fingerprinted above.
    runtime_config_sha256: str | None = None
    manifest_sha256 = _prediction_manifest_sha256(identities)
    prediction_total_bytes = sum(item.size_bytes for item in identities)

    # The first durable record is written immediately after exclusive mkdir; all following writes are guarded.
    output.mkdir()
    stdout_path = output / "stdout.txt"
    stderr_path = output / "stderr.txt"
    summary_path = output / "runtime-summary.json"
    result_dir = output / "result"
    config_for_run = output / "omnidocbench-runtime.yaml"
    staged_predictions = output / "input" / "predictions"
    staged_annotation = output / "input" / "annotations.json"
    runtime_config_sha256: str | None = None
    staging_seconds = 0.0
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    exit_code: int | None = None
    error: str | None = None
    status = "running"
    termination_status = "not_started"

    def make_result() -> OmniDocBenchRunResult:
        return OmniDocBenchRunResult(
            status=status, scorer_commit=actual_commit, config_sha256=actual_config_sha256,
            runtime_config_sha256=runtime_config_sha256, annotation_sha256=annotation_sha256,
            prediction_manifest=identities, prediction_manifest_sha256=manifest_sha256,
            prediction_file_count=len(identities), prediction_total_bytes=prediction_total_bytes,
            exit_code=exit_code, runtime_seconds=round(time.monotonic() - started, 6),
            termination_status=termination_status, stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            scorer_result_dir=str(result_dir) if result_dir.is_dir() else None, error=error,
            summary_path=str(summary_path),
            staged_input_dir=str(output / "input") if (output / "input").exists() else None,
            staging_seconds=round(staging_seconds, 6),
        )

    try:
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        _write_summary(summary_path, make_result())
        staging_started = time.monotonic()
        staged_predictions, staged_annotation = _stage_inputs(
            output, prediction, annotation, entries, annotation_sha256,
            max_prediction_bytes=max_prediction_bytes, max_annotation_bytes=max_annotation_bytes,
        )
        if not _verify_staged_inputs(staged_predictions, staged_annotation, entries, annotation_sha256):
            raise ValueError("Staged evaluation inputs failed post-copy integrity verification")
        staging_seconds = time.monotonic() - staging_started
        runtime_config = _load_runtime_config(
            config, staged_predictions, staged_annotation, max_workers=max_workers
        )
        runtime_config_sha256 = _sha256_bytes(runtime_config)
        config_for_run.write_bytes(runtime_config)
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                [sys.executable, str(entrypoint), "--config", str(config_for_run)],
                cwd=output,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_stream,
                stderr=stderr_stream,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            termination_status = "running"
            try:
                exit_code = process.wait(timeout=timeout_seconds)
                termination_status = "not_needed"
                status = "succeeded" if exit_code == 0 else "failed"
                if exit_code != 0:
                    error = f"Official scorer exited with code {exit_code}"
                elif not result_dir.is_dir() or not any(result_dir.iterdir()):
                    status = "failed"
                    error = "Official scorer exited successfully but produced no result artifacts"
            except subprocess.TimeoutExpired:
                termination_status = _terminate_process_tree(process)
                status = "failed" if termination_status == "terminated" else "indeterminate"
                error = f"Official scorer exceeded timeout of {timeout_seconds} seconds"
                if status == "indeterminate":
                    error += "; scorer process-tree termination could not be confirmed"
    except Exception as exc:
        if process is not None and process.poll() is None:
            termination_status = _terminate_process_tree(process)
            status = "indeterminate" if termination_status != "terminated" else "failed"
        else:
            status = "failed"
        error = f"Official scorer run failed: {type(exc).__name__}: {exc}"
    finally:
        try:
            if config_for_run.exists():
                config_for_run.unlink()
        except OSError as exc:
            status = "indeterminate"
            error = f"Could not remove generated runtime config: {exc}"
        for log_path in (stdout_path, stderr_path):
            try:
                log_path.touch(exist_ok=True)
            except OSError as exc:
                status = "indeterminate"
                error = f"Could not persist scorer output log {log_path.name}: {exc}"
        if process is not None and staged_predictions.exists() and not _verify_staged_inputs(
            staged_predictions, staged_annotation, entries, annotation_sha256
        ):
            status = "indeterminate"
            error = "Staged evaluation inputs changed during scoring; result cannot be trusted"
        result = make_result()
        try:
            _write_summary(summary_path, result)
        except OSError as exc:
            # Preserve the initial running record when possible; don't claim an auditable terminal state.
            if not summary_path.exists():
                temporary = summary_path.with_suffix(".json.tmp")
                for orphan in (temporary, stdout_path, stderr_path):
                    try:
                        orphan.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    output.rmdir()
                except OSError:
                    pass
            raise RuntimeError(
                f"Could not persist terminal scorer summary at {summary_path}; run status is indeterminate"
            ) from exc
    return result
