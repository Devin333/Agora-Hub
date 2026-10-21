"""Application orchestration for immutable, offline QASPER retrieval experiments."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from backend.research.application.qasper_assets import QasperAssetProvider
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.rag.evaluation.protocol_common import (
    EvaluationTrack, ExecutionProfile, PROTOCOL_VERSION, SCHEMA_VERSION, content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import QuestionScope, ScopedPaper
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    calculate_index_content_sha256, text_sha256,
)
from backend.research.rag.evaluation.protocol_execution import (
    ArtifactReference, BudgetLimits, CachePolicy, CaseScopeProjection, ComparisonIdentity,
    ExecutionEnvironment, ExperimentManifest, ExperimentPurpose, InitialState, InitialStatePolicy,
    IsolatedNamespaces, PlannedEvaluationUnit, RetrievalFactors, VariantConfiguration,
)
from backend.research.rag.evaluation.qasper_experiments import (
    PROFILES, ExperimentCase, RetrievalProfile, SweepParameters, build_sweep, retrieve_profile,
)
from backend.research.rag.evaluation.qasper_mapping import FrozenQasperDataset, freeze_qasper_dataset
from backend.research.rag.evaluation.qasper_diagnostics import (
    QasperExecutionTelemetry, QasperLatencyObservation, QasperUnitTelemetry,
    attribute_qasper_retrieval_failures, diagnose_qasper_report,
)
from backend.research.rag.evaluation.qasper_protocol import (
    EQUIVALENT_METRIC_VERSION, QASPER_MAPPING_VERSION, STRICT_METRIC_VERSION,
    QasperCaseResult, QasperDataIdentity, QasperDenominators, QasperExecutionIdentity,
    QasperExperimentManifest,
    QasperFailureRecord, QasperMappingResult, QasperMetric, QasperMetricDefinition,
    QasperMetricVersions, QasperMappingPolicy, QasperReportArtifact,
    QasperReportFiles, RankedResult, QasperChannelProvenance, RetrievalStage,
    FIXTURE_EXECUTION_VERSION,
)
from backend.research.rag.evaluation.qasper_scoring import score_qasper_metrics

ManifestFactory = Callable[[FrozenQasperDataset, object, object, tuple[ExperimentCase, ...]], QasperExperimentManifest]


def build_qasper_experiment_manifest(frozen: FrozenQasperDataset, assets, cache_identity,
                                    experiments: tuple[ExperimentCase, ...]) -> QasperExperimentManifest:
    """Construct the complete shared execution manifest from observed local inputs."""
    if not experiments:
        raise ValueError("at least one explicit sweep combination is required")
    binding = frozen.binding
    docs = {document.paper_id: document for document in assets.documents}
    scopes = []
    for case in frozen.cases:
        document = docs[case.paper_id]
        source_digest = content_digest(document.model_dump(mode="json"))
        version = str(document.source_snapshot_id or source_digest)
        scope = QuestionScope(sources=(ScopedPaper(paper_id=case.paper_id,
            document_version=version, source_sha256=source_digest),))
        scopes.append(CaseScopeProjection.create(case_id=case.case_id, scope=scope))
    scope_tuple = tuple(scopes)
    env = ExecutionEnvironment(runtime=f"python-{__import__('platform').python_version()}",
        dependency_lock_digest=content_digest({"python": __import__('platform').python_version(),
                                               "platform": __import__('platform').platform()}),
        hardware_class=None, hardware_identity_unavailable_reason="local fixture hardware is not normalized")
    budget = BudgetLimits(max_requests=0, max_total_tokens=0, max_cost=None, currency=None,
        unknown_price=True, unknown_price_reason="offline fixture execution has no external provider price",
        price_basis_reference=None, request_timeout_seconds=60, case_deadline_seconds=3600,
        run_deadline_seconds=86400, max_concurrency=1, max_retries=0, max_replans=0, max_turns=1)
    initial = InitialState(memory_policy=InitialStatePolicy.EMPTY, memory_snapshot_digest=None,
        cache_policy=CachePolicy.COLD, cache_snapshot_digest=None, gold_labels_present=False,
        prior_run_answers_present=False, production_business_memory_mounted=False, source_fulltext_access="read_only")
    namespaces = IsolatedNamespaces(tenant="qasper-offline-fixture", index="qasper-fixture-index",
        memory="qasper-fixture-memory", artifact="qasper-fixture-artifact")
    code = assets.code_identity
    variants = []
    comparisons = []
    plan = []
    for experiment in experiments:
        params = experiment.parameters.as_dict()
        profile = experiment.profile
        retrieval = RetrievalFactors(parser_config_hash=binding.parser_config_sha256,
            chunker_config_hash=binding.chunker_config_sha256,
            embedding_config_hash=content_digest({"model": cache_identity.model_id,
                                                   "version": cache_identity.model_version,
                                                   "dimension": cache_identity.dimension}),
            index_config_hash=binding.index_config_sha256,
            retriever_config_hash=content_digest({"profile": profile.__dict__, "parameters": params}),
            reranker_config_hash=(content_digest("LightweightLexicalReranker/v1")
                if params["rerank_score_threshold"] is not None else None),
            reranker_not_applicable_reason=(None if params["rerank_score_threshold"] is not None
                else "Reranking is disabled for this sweep combination."),
            context_policy_hash=content_digest({"retrieval_only": True, "question_scope_only": True}))
        variant = VariantConfiguration.create(variant_id=experiment.fingerprint,
            track=EvaluationTrack.RETRIEVAL, retrieval=retrieval, retrieval_not_applicable_reason=None,
            generation=None, generation_not_applicable_reason="Retrieval-only evaluation.",
            harness=None, harness_not_applicable_reason="Harness is not part of retrieval-only evaluation.")
        variants.append(variant)
        for case in frozen.cases:
            plan.append(PlannedEvaluationUnit(dataset_version=frozen.dataset_version, case_id=case.case_id,
                track=EvaluationTrack.RETRIEVAL, variant_fingerprint=variant.variant_fingerprint, repetition=1))
        comparisons.append(ComparisonIdentity.create(dataset_digest=frozen.dataset_digest,
            case_scope_fingerprint=content_digest(scope_tuple), corpus_content_sha256=frozen.corpus_sha256,
            track=EvaluationTrack.RETRIEVAL, execution_profile=ExecutionProfile.FIXTURE,
            variant_fingerprint=variant.variant_fingerprint, code_fingerprint=code.code_fingerprint,
            environment_fingerprint=env.fingerprint, budget_fingerprint=budget.fingerprint,
            initial_state_fingerprint=initial.fingerprint, namespace_fingerprint=namespaces.fingerprint,
            repetition_plan=(1,), timing_boundary="single offline fixture retrieval request; excludes no network calls"))
    shared = ExperimentManifest.create(record_type="experiment_manifest", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, experiment_id=f"qasper-{frozen.digest[:16]}",
        experiment_purpose=ExperimentPurpose.DEV_DIAGNOSTIC, dataset_id=frozen.dataset_id,
        dataset_version=frozen.dataset_version, dataset_digest=frozen.dataset_digest,
        case_scopes=scope_tuple, variants=tuple(variants), planned_units=tuple(plan),
        comparison_identities=tuple(comparisons), allowed_differences=())
    definitions = tuple(
        QasperMetricDefinition(metric_id=metric, version=version, k=k)
        for metric, version, k in (
            ("strict_hit", STRICT_METRIC_VERSION, 5),
            ("equivalent_hit", EQUIVALENT_METRIC_VERSION, 5),
            ("strict_hit", STRICT_METRIC_VERSION, 10),
            ("equivalent_hit", EQUIVALENT_METRIC_VERSION, 10),
            ("strict_coverage", STRICT_METRIC_VERSION, 5),
            ("equivalent_coverage", EQUIVALENT_METRIC_VERSION, 5),
            ("strict_coverage", STRICT_METRIC_VERSION, 10),
            ("equivalent_coverage", EQUIVALENT_METRIC_VERSION, 10),
            ("strict_mrr", STRICT_METRIC_VERSION, None),
            ("equivalent_mrr", EQUIVALENT_METRIC_VERSION, None),
            ("strict_ndcg", STRICT_METRIC_VERSION, 5),
            ("equivalent_ndcg", EQUIVALENT_METRIC_VERSION, 5),
            ("strict_ndcg", STRICT_METRIC_VERSION, 10),
            ("equivalent_ndcg", EQUIVALENT_METRIC_VERSION, 10),
        )
    )
    cache_sha = cache_identity.sha256
    data = QasperDataIdentity(dataset_id=frozen.dataset_id, dataset_version=frozen.dataset_version,
        dataset_digest=frozen.dataset_digest, dataset_sha256=assets.dataset_sha256,
        dataset_case_count=len(frozen.cases),
        evaluable_case_count=sum(case.mapping_result.disposition == "valid" for case in frozen.cases),
        corpus_snapshot_id=binding.corpus_snapshot_id, corpus_sha256=frozen.corpus_sha256,
        chunk_snapshot_id=binding.index_snapshot_id, chunk_sha256=frozen.index_sha256,
        chunking_version=binding.chunker_version, chunk_count=len(assets.chunks),
        paper_count=len(assets.documents), embedding_cache_id=cache_identity.sha256,
        embedding_cache_sha256=cache_sha, embedding_model_id=cache_identity.model_id,
        embedding_model_version=cache_identity.model_version, embedding_dimension=cache_identity.dimension)
    mapping_payload = _json_bytes(frozen.model_dump(mode="json"))
    mapping_sha = hashlib.sha256(mapping_payload).hexdigest()
    return QasperExperimentManifest.create(experiment_manifest=shared, data_identity=data,
        index_binding=binding, mapping_artifact=ArtifactReference(reference_id=f"qasper/mapping/{mapping_sha}",
            kind="qasper_frozen_mapping", uri=f"external-sha256://{mapping_sha}", content_sha256=mapping_sha),
        mapping_policy=QasperMappingPolicy(), metric_versions=QasperMetricVersions(),
        metric_definitions=definitions, execution_identity=QasperExecutionIdentity(
            execution_profile=ExecutionProfile.FIXTURE, version=FIXTURE_EXECUTION_VERSION,
            implementation_id="offline-qasper-retrieval/v1",
            configuration_sha256=content_digest({"profiles": [item.profile.name for item in experiments],
                                                   "parameters": [item.parameters.as_dict() for item in experiments]})),
        code_identity=code)


@dataclass(frozen=True)
class QasperEvaluationConfig:
    """Explicit experiment inputs; the factory must build the full shared manifest DTO."""

    output_directory: str = ""
    manifest_factory: ManifestFactory | None = None
    candidate_limits: tuple[int, ...] = (100,)
    overfetch_multipliers: tuple[int, ...] = (3,)
    rrf_ks: tuple[int, ...] = (60,)
    rerank_score_thresholds: tuple[float | None, ...] = (None,)
    channel_weights: tuple[Mapping[str, float], ...] = ({},)
    rerank_enabled: tuple[bool, ...] = (False,)
    run_id: str | None = None


class QasperEvaluationService:
    def __init__(self, assets: QasperAssetProvider, artifacts: QasperArtifactStore, config: QasperEvaluationConfig):
        self._assets, self._artifacts, self._config = assets, artifacts, config

    def freeze_mapping(self, output_dir: str | Path | None = None) -> FrozenQasperDataset:
        directory = self._directory(output_dir)
        assets = self._assets.read_assets()
        frozen = freeze_qasper_dataset(assets.raw_dataset, assets.documents, assets.chunks)
        frozen.verify_digest()
        payload = _json_bytes(frozen.model_dump(mode="json"))
        self._artifacts.publish(directory, {"mapping.json": payload})
        # Re-read the immutable evidence and validate what was actually persisted.
        persisted = FrozenQasperDataset.model_validate_json(self._artifacts.read(directory, "mapping.json"))
        persisted.verify_digest()
        if persisted.digest != frozen.digest:
            raise ValueError("persisted frozen mapping differs from the mapping just constructed")
        return persisted

    def run_sweep(self, *, selected_profiles: Sequence[str] | None = None,
                  mapping_directory: str | Path | None = None,
                  output_directory: str | Path | None = None,
                  retain_reports: bool = True,
                  publish_run_index: bool = True) -> tuple[QasperReportArtifact, ...]:
        output = self._directory(output_directory)
        if mapping_directory is None:
            frozen = self.freeze_mapping(output)
        else:
            frozen = FrozenQasperDataset.model_validate_json(self._artifacts.read(str(mapping_directory), "mapping.json"))
            frozen.verify_digest()
        mapping_bytes = self._artifacts.read(
            str(mapping_directory) if mapping_directory is not None else output,
            "mapping.json",
        )
        assets = self._assets.read_assets()
        _validate_frozen_assets(frozen, assets)
        self._artifacts.publish(output, {"mapping.json": mapping_bytes})
        cache_identity = self._assets.cache_identity()
        profile_names = tuple(selected_profiles or PROFILES.keys())
        if not profile_names or len(set(profile_names)) != len(profile_names) or any(name not in PROFILES for name in profile_names):
            raise ValueError("selected_profiles must be unique known QASPER retrieval profiles")
        identity = {
            "code": assets.code_identity.code_fingerprint,
            "dataset": frozen.dataset_digest,
            "index": frozen.index_sha256,
            "embedding": cache_identity.sha256,
            "artifact": frozen.digest,
        }
        experiments = _build_experiments(profile_names, self._config, identity)
        if retain_reports and len(experiments) > 8:
            raise ValueError(
                "run_sweep retains report objects and is limited to eight variants; "
                "use sweep() for bounded streaming execution"
            )
        manifest_factory = self._config.manifest_factory or build_qasper_experiment_manifest
        manifest = manifest_factory(frozen, assets, cache_identity, experiments)
        if not isinstance(manifest, QasperExperimentManifest):
            raise TypeError("manifest_factory must return a fully validated QasperExperimentManifest")
        manifest.verify_digest()
        if manifest.data_identity.dataset_digest != frozen.dataset_digest or manifest.index_binding != frozen.binding:
            raise ValueError("experiment manifest does not bind the frozen mapping dataset/index")
        if manifest.mapping_artifact.content_sha256 != hashlib.sha256(mapping_bytes).hexdigest():
            raise ValueError("experiment manifest mapping artifact does not identify the persisted frozen mapping")
        run_id = self._config.run_id or str(uuid.uuid4())
        reports: list[QasperReportArtifact] = []
        all_valid = True
        for experiment in experiments:
            variant = next((v for v in manifest.experiment_manifest.variants
                            if v.variant_id == experiment.fingerprint), None)
            if variant is None:
                raise ValueError(f"manifest does not declare sweep combination {experiment.fingerprint}")
            units = tuple(u for u in manifest.experiment_manifest.planned_units
                          if u.variant_fingerprint == variant.variant_fingerprint)
            expected_cases = {case.case_id for case in frozen.cases}
            if len(units) != len(expected_cases) or {unit.case_id for unit in units} != expected_cases:
                raise ValueError(f"manifest plan for sweep combination {experiment.fingerprint} must include every frozen case exactly once")
            units_by_case = {unit.case_id: unit for unit in units}
            cases = tuple(self._evaluate_case(frozen, case, units_by_case[case.case_id],
                profile=experiment.profile, parameters=experiment.parameters, manifest=manifest,
                run_id=run_id, output_directory=output) for case in frozen.cases)
            report = self._report(
                manifest, variant.variant_fingerprint, run_id, cases, experiment
            )
            self._publish_report(
                report, experiment.fingerprint, experiment, directory=output
            )
            all_valid = all_valid and report.denominators.system_failure == 0
            if retain_reports:
                reports.append(report)
            else:
                del report, cases
        index = {
            "run_id": run_id,
            "profiles": [item.fingerprint for item in experiments],
            "manifest_digest": manifest.digest,
        }
        context = {
            case.case_id: {
                "paper_id": case.paper_id,
                "question_type": case.question_type,
                "sections": list(case.sections),
                "mapping_status": case.mapping_result.disposition,
                "evidence_count": len(case.strict_anchors),
            }
            for case in frozen.cases
        }
        experiment_index = {
            item.fingerprint: {
                "profile": item.profile.name,
                "parameters": item.parameters.as_dict(),
            }
            for item in experiments
        }
        run_summary = {
            "run_id": run_id,
            "experiments": len(experiments),
            "valid": all_valid,
        }
        if publish_run_index:
            self._artifacts.publish(
                output,
                {
                    "run-index.json": _json_bytes(index),
                    "run-context.json": _json_bytes(context),
                    "run-experiments.json": _json_bytes(experiment_index),
                    "run-summary.json": _json_bytes(run_summary),
                },
            )
        return tuple(reports)

    def read_report(self, profile: str) -> QasperReportArtifact:
        if not profile or Path(profile).name != profile:
            raise ValueError("profile must be a portable sweep-combination identifier")
        data = self._artifacts.read(self._directory(), f"{profile}-report.json")
        report = QasperReportArtifact.model_validate_json(data)
        report.manifest.verify_digest()
        _verify_report_artifacts(self._artifacts, self._directory(), profile, report)
        if report.selected_variant_fingerprint not in {v.variant_fingerprint for v in report.manifest.experiment_manifest.variants}:
            raise ValueError("report selected variant is absent from manifest")
        return report

    def _directory(self, value: str | Path | None = None) -> str:
        candidate = str(value) if value is not None else self._config.output_directory
        if not candidate or not candidate.strip():
            raise ValueError("an explicit output or run directory is required")
        return candidate

    def validate_mapping(self, mapping_dir: str | Path) -> dict[str, object]:
        directory = self._directory(mapping_dir)
        frozen = FrozenQasperDataset.model_validate_json(self._artifacts.read(directory, "mapping.json"))
        frozen.verify_digest()
        current = freeze_qasper_dataset(self._assets.read_assets().raw_dataset,
            self._assets.read_assets().documents, self._assets.read_assets().chunks)
        if frozen.digest != current.digest:
            raise ValueError("frozen mapping does not match the explicitly configured local dataset/corpus")
        return {"valid": True, "mapping_digest": frozen.digest, "case_count": len(frozen.cases)}

    def run(self, output_dir: str | Path, mapping_dir: str | Path, profile: str,
            parameters: Mapping[str, object]) -> dict[str, object]:
        candidate_limit = _positive_int(parameters.get("candidate_limit"), "candidate_limit")
        overfetch = _positive_int(parameters.get("overfetch"), "overfetch")
        rrf_k = _positive_int(parameters.get("rrf_k"), "rrf_k")
        if type(parameters.get("rerank")) is not bool:
            raise ValueError("rerank must be an explicit boolean")
        weights = parameters.get("weights")
        if not isinstance(weights, Mapping):
            raise ValueError("weights must be an explicit channel-weight mapping")
        threshold = _finite_number(parameters.get("threshold"), "threshold")
        config = replace(self._config, output_directory=str(output_dir), candidate_limits=(candidate_limit,),
            overfetch_multipliers=(_positive_int(overfetch, "overfetch"),), rrf_ks=(rrf_k,),
            rerank_score_thresholds=((threshold,) if parameters["rerank"] else (None,)),
            channel_weights=(weights,), rerank_enabled=(parameters["rerank"],))
        result = QasperEvaluationService(self._assets, self._artifacts, config).run_sweep(
            selected_profiles=(profile,), mapping_directory=mapping_dir, output_directory=output_dir)
        report = result[0]
        experiment_id = next(
            (
                variant.variant_id
                for variant in report.manifest.experiment_manifest.variants
                if variant.variant_fingerprint == report.selected_variant_fingerprint
            ),
            "",
        )
        return {"valid": report.denominators.system_failure == 0, "run_id": report.run_id,
                "profile": profile, "experiment_id": experiment_id, "planned": report.denominators.planned,
                "valid_cases": report.denominators.valid, "needs_review": report.denominators.needs_review,
                "system_failure": report.denominators.system_failure, "external_requests": 0,
                "external_cost": None, "latency_semantics": "offline fixture; not production latency"}

    def sweep(self, output_dir: str | Path, mapping_dir: str | Path, grid: Mapping[str, Sequence[object]],
              profiles: Sequence[str]) -> dict[str, object]:
        required = {"candidate_limit", "overfetch", "rrf_k", "rerank", "threshold", "weights"}
        if set(grid) != required or any(not grid[key] for key in required):
            raise ValueError("sweep grid must explicitly provide every CLI parameter dimension")
        if any(type(value) is not bool for value in grid["rerank"]):
            raise ValueError("rerank grid entries must be booleans")
        if any(not isinstance(value, Mapping) for value in grid["weights"]):
            raise ValueError("weights grid entries must be mappings")
        if any(value is not None and not isinstance(value, (int, float)) for value in grid["threshold"]):
            raise ValueError("threshold grid entries must be finite numeric values")
        config = replace(self._config, output_directory=str(output_dir),
            candidate_limits=tuple(_positive_int(x, "candidate_limit") for x in grid["candidate_limit"]),
            overfetch_multipliers=tuple(_positive_int(x, "overfetch") for x in grid["overfetch"]),
            rrf_ks=tuple(_positive_int(x, "rrf_k") for x in grid["rrf_k"]),
            rerank_score_thresholds=tuple(
                None if x is None else _finite_number(x, "threshold")
                for x in grid["threshold"]
            ),
            channel_weights=tuple(grid["weights"]), rerank_enabled=tuple(grid["rerank"]))

        frozen = FrozenQasperDataset.model_validate_json(
            self._artifacts.read(str(mapping_dir), "mapping.json")
        )
        frozen.verify_digest()
        assets = self._assets.read_assets()
        _validate_frozen_assets(frozen, assets)
        cache_identity = self._assets.cache_identity()
        profile_names = tuple(profiles)
        if not profile_names or len(set(profile_names)) != len(profile_names) or any(
            name not in PROFILES for name in profile_names
        ):
            raise ValueError("profiles must be unique known QASPER retrieval profiles")
        identity = {
            "code": assets.code_identity.code_fingerprint,
            "dataset": frozen.dataset_digest,
            "index": frozen.index_sha256,
            "embedding": cache_identity.sha256,
            "artifact": frozen.digest,
        }
        experiments = _build_experiments(profile_names, config, identity)
        run_id = config.run_id or str(uuid.uuid4())
        identifiers = []
        experiment_index = {}
        manifest_digests = {}
        all_valid = True
        for experiment in experiments:
            threshold = experiment.parameters.rerank_score_threshold
            single_config = replace(
                config,
                run_id=run_id,
                candidate_limits=(experiment.parameters.candidate_limit,),
                overfetch_multipliers=(experiment.parameters.overfetch_multiplier,),
                rrf_ks=(experiment.parameters.rrf_k,),
                rerank_score_thresholds=(threshold,),
                channel_weights=(dict(experiment.parameters.channel_weights),),
                rerank_enabled=(threshold is not None,),
            )
            (report,) = QasperEvaluationService(
                self._assets, self._artifacts, single_config
            ).run_sweep(
                selected_profiles=(experiment.profile.name,),
                mapping_directory=mapping_dir,
                output_directory=output_dir,
                retain_reports=True,
                publish_run_index=False,
            )
            identifiers.append(experiment.fingerprint)
            experiment_index[experiment.fingerprint] = {
                "profile": experiment.profile.name,
                "parameters": experiment.parameters.as_dict(),
            }
            manifest_digests[experiment.fingerprint] = report.manifest.digest
            all_valid = all_valid and report.denominators.system_failure == 0
            del report
        context = {
            case.case_id: {
                "paper_id": case.paper_id,
                "question_type": case.question_type,
                "sections": list(case.sections),
                "mapping_status": case.mapping_result.disposition,
                "evidence_count": len(case.strict_anchors),
            }
            for case in frozen.cases
        }
        index = {
            "run_id": run_id,
            "profiles": identifiers,
            "manifest_digests": manifest_digests,
        }
        summary = {
            "run_id": run_id,
            "experiments": len(identifiers),
            "valid": all_valid,
        }
        self._artifacts.publish(
            str(output_dir),
            {
                "run-index.json": _json_bytes(index),
                "run-context.json": _json_bytes(context),
                "run-experiments.json": _json_bytes(experiment_index),
                "run-summary.json": _json_bytes(summary),
            },
        )
        return {"valid": all_valid, "run_id": run_id,
                "experiments": len(identifiers), "profiles": list(profiles),
                "external_requests": 0, "external_cost": None,
                "latency_semantics": "offline fixture; not production latency"}

    def score(self, run_dir: str | Path) -> dict[str, object]:
        directory = self._directory(run_dir)
        index = _load_run_index(self._artifacts, directory)
        frozen = FrozenQasperDataset.model_validate_json(
            self._artifacts.read(directory, "mapping.json")
        )
        frozen.verify_digest()
        frozen_cases = {case.case_id: case for case in frozen.cases}
        validated_cases = 0
        run_id: str | None = None
        for identifier in index:
            report = QasperReportArtifact.model_validate_json(
                self._artifacts.read(directory, f"{identifier}-report.json")
            )
            report.manifest.verify_digest()
            _verify_report_artifacts(
                self._artifacts, directory, identifier, report
            )
            if run_id is None:
                run_id = report.run_id
            elif run_id != report.run_id:
                raise ValueError("variant reports belong to different run IDs")
            for case in report.case_results:
                frozen_case = frozen_cases.get(case.unit.case_id)
                if frozen_case is None:
                    raise ValueError(
                        f"run mapping lacks planned case {case.unit.case_id}"
                    )
                if case.status == "ok":
                    rescored = score_qasper_metrics(
                        frozen_case,
                        case.raw_ranked_results,
                        report.manifest.metric_definitions,
                        final_stage=case.final_stage,
                    )
                    if rescored != case.metrics:
                        raise ValueError(
                            "persisted scores disagree with deterministic "
                            f"rescoring for case {case.unit.case_id}"
                        )
                elif not case.failures or any(
                    metric.status != case.status or metric.value is not None
                    for metric in case.metrics
                ):
                    raise ValueError(
                        f"non-measured case {case.unit.case_id} lacks a consistent failure record"
                    )
                validated_cases += 1
            del report
        payload = {
            "run_id": run_id,
            "profiles": list(index),
            "validated_cases": validated_cases,
            "valid": True,
        }
        self._artifacts.publish(directory, {"score.json": _json_bytes(payload)})
        return payload

    def report(self, run_dir: str | Path) -> dict[str, object]:
        directory = self._directory(run_dir)
        score_result = self.score(directory)
        index = _load_run_index(self._artifacts, directory)
        frozen = FrozenQasperDataset.model_validate_json(
            self._artifacts.read(directory, "mapping.json")
        )
        frozen.verify_digest()
        experiment_index = json.loads(
            self._artifacts.read(directory, "run-experiments.json")
        )
        if set(experiment_index) != set(index):
            raise ValueError("run experiment identities differ from run index")

        experiment_summaries = []
        for identifier in index:
            item = QasperReportArtifact.model_validate_json(
                self._artifacts.read(directory, f"{identifier}-report.json")
            )
            units = []
            channel_order: tuple[str, ...] | None = None
            for case in item.case_results:
                artifact_name = (
                    f"{case.unit.variant_fingerprint}-"
                    f"{content_digest(case.unit.case_id)}-execution.json"
                )
                payload = json.loads(self._artifacts.read(directory, artifact_name))
                stages = tuple(payload.get("executed_stages", ()))
                if payload.get("final_stage") != case.final_stage:
                    raise ValueError("execution telemetry final_stage differs from case result")
                current_order = tuple(payload.get("channel_order", ()))
                if channel_order is None:
                    channel_order = current_order
                elif channel_order != current_order:
                    raise ValueError("execution telemetry channel order changed within one variant")
                units.append(
                    QasperUnitTelemetry(
                        case_id=case.unit.case_id,
                        repetition=case.unit.repetition,
                        attempt_id=case.attempt_id,
                        executed_stages=stages,
                        latencies=(
                            QasperLatencyObservation(
                                phase="request",
                                milliseconds=float(payload["fixture_elapsed_ms"]),
                            ),
                        ),
                        rerank_input_chunk_ids=(
                            tuple(payload["rerank_input_chunk_ids"])
                            if payload.get("rerank_input_chunk_ids") is not None
                            else None
                        ),
                    )
                )
            telemetry = QasperExecutionTelemetry(
                run_id=item.run_id,
                execution_profile=item.manifest.execution_identity.execution_profile,
                channel_order=channel_order or (),
                units=tuple(units),
                index_cost_usd=0.0,
                request_cost_usd=0.0,
                judge_cost_usd=0.0,
            )
            diagnostics = {}
            for policy in ("strict", "equivalent"):
                for k in (5, 10):
                    diagnostic = diagnose_qasper_report(
                        frozen, item, telemetry, policy=policy, k=k
                    )
                    diagnostics[f"{policy}@{k}"] = {
                        "policy": policy,
                        "k": k,
                        "question_type_derivation_id": diagnostic.question_type_derivation_id,
                        "question_type_note": diagnostic.question_type_note,
                        "question_type_source_counts": {
                            source: sum(
                                case.question_type_source == source
                                for case in diagnostic.cases
                            )
                            for source in ("dataset", "derived_not_gold", "unknown")
                        },
                        "denominators": diagnostic.denominators.model_dump(mode="json"),
                        "slices": [
                            {
                                "dimension": value.dimension,
                                "value": value.value,
                                "denominators": value.denominators.model_dump(mode="json"),
                                "diagnosed_count": value.diagnosed_count,
                                "hit_count": value.hit_count,
                                "metrics": [
                                    metric.model_dump(mode="json")
                                    for metric in value.metrics
                                ],
                                "attributions": list(value.attributions),
                            }
                            for value in diagnostic.slices
                        ],
                        "latency": [
                            value.model_dump(mode="json")
                            for value in diagnostic.latency
                        ],
                        "external_costs": [
                            value.model_dump(mode="json")
                            for value in diagnostic.external_costs
                        ],
                        "attribution_counts": dict(
                            sorted(
                                (cause, sum(case.attribution == cause for case in diagnostic.cases))
                                for cause in {case.attribution for case in diagnostic.cases}
                            )
                        ),
                    }
                    del diagnostic
            failures = [
                failure.model_dump(mode="json")
                for case in item.case_results
                for failure in case.failures
            ]
            identity = experiment_index[identifier]
            experiment_summaries.append(
                {
                    "experiment_id": identifier,
                    "profile": identity["profile"],
                    "parameters": identity["parameters"],
                    "selected_variant_fingerprint": item.selected_variant_fingerprint,
                    "denominators": item.denominators.model_dump(mode="json"),
                    "metrics": [
                        metric.model_dump(mode="json") for metric in item.metrics
                    ],
                    "diagnostics": diagnostics,
                    "failures": failures,
                    "failure_attribution": _failure_counts(item.case_results),
                }
            )
            del item, telemetry, units
        summary = {
            "run_id": score_result["run_id"],
            "execution_profile": "fixture",
            "experiments": experiment_summaries,
            "external_requests": 0,
            "external_cost": None,
            "latency_semantics": (
                "offline fixture wall-clock; not production latency"
            ),
        }
        report_json = _json_bytes(summary)
        report_markdown = _run_report_markdown(summary)
        self._artifacts.publish(
            directory,
            {"report.json": report_json, "report.md": report_markdown},
        )
        if self._artifacts.read(directory, "report.json") != report_json:
            raise ValueError("published report.json failed read-after-write verification")
        if self._artifacts.read(directory, "report.md") != report_markdown:
            raise ValueError("published report.md failed read-after-write verification")
        return {
            "valid": True,
            "run_id": summary["run_id"],
            "experiments": len(index),
            "report_artifact": "report.json",
            "human_report_artifact": "report.md",
            "failure_count": sum(
                len(item["failures"]) for item in summary["experiments"]
            ),
        }

    def _evaluate_case(self, frozen: FrozenQasperDataset, case, unit: PlannedEvaluationUnit, *,
                       profile: RetrievalProfile, parameters: SweepParameters,
                       manifest: QasperExperimentManifest, run_id: str,
                       output_directory: str) -> QasperCaseResult:
        definitions = manifest.metric_definitions
        mapping_result = case.mapping_result
        started = time.perf_counter()
        ranked: tuple[RankedResult, ...] = ()
        final_stage: RetrievalStage = "candidate"
        failure_records: tuple[QasperFailureRecord, ...] = ()
        status = "ok"
        reason = None
        try:
            if mapping_result.disposition != "valid":
                raise _CaseFailure(mapping_result.disposition, "mapping", mapping_result.reason or "mapping is not valid")
            channel_inputs = self._assets.retrieval_inputs(
                self._assets.read_assets().chunks, dense=profile.semantic or profile.field, field=profile.field)
            gold_ids = tuple(sorted({chunk_id for anchor in mapping_result.mapping.anchor_mappings
                                     for chunk_set in anchor.chunk_sets for chunk_id in chunk_set.chunk_ids}))
            retrieval = retrieve_profile(profile=profile, inputs=channel_inputs, paper_id=case.paper_id,
                question=case.query, policy=_OfflinePolicy(), parameters=parameters,
                reranker=_lexical_rerank if parameters.rerank_score_threshold is not None else None,
                reranker_id=("lightweight-lexical/v1" if parameters.rerank_score_threshold is not None else None),
                frozen_gold_evidence_ids=gold_ids)
            final_stage: RetrievalStage = (
                "reranked" if parameters.rerank_score_threshold is not None else "fused"
            )
            ranked = _ranked_rows(
                retrieval, reranked=final_stage == "reranked"
            )
            metrics = score_qasper_metrics(
                case, ranked, definitions, final_stage=final_stage
            )
            if any(item.status != "ok" for item in metrics):
                raise _CaseFailure("needs_review", "scoring", "frozen mapping is insufficient for deterministic scoring")
            failure_records = attribute_qasper_retrieval_failures(
                case, unit, ranked, final_stage, metrics
            )
        except _CaseFailure as exc:
            status, reason = exc.status, exc.reason
            metrics = _unmeasured(definitions, status, reason)
            category = ("not_applicable" if status == "not_applicable" else
                        "mapping_failure" if exc.stage == "mapping" else "system_failure")
            failure_records = (QasperFailureRecord(unit=unit, category=category,
                status=status, reason=reason, stage=exc.stage, affected_metrics=definitions),)
        except Exception as exc:
            status, reason = "system_failure", f"{type(exc).__name__}: {exc}"
            metrics = _unmeasured(definitions, status, reason)
            failure_records = (QasperFailureRecord(unit=unit, category="system_failure", status=status, reason=reason,
                stage="index", affected_metrics=definitions),)
        # Fixture cache rows are offline inputs. They do not measure production request latency or external cost.
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        executed_stages = (
            ("candidate", "fused", "reranked")
            if final_stage == "reranked"
            else ("candidate", "fused")
            if final_stage == "fused"
            else ()
        )
        channel_order = tuple(
            channel
            for enabled, channel in (
                (profile.semantic, "dense"),
                (profile.bm25, "bm25"),
                (profile.field, "field"),
            )
            if enabled
        )
        case_payload = {
            "fixture_elapsed_ms": elapsed_ms,
            "external_requests": 0,
            "external_cost": None,
            "latency_semantics": "offline fixture execution; not production latency",
            "unit": unit.model_dump(mode="json"),
            "executed_stages": executed_stages,
            "final_stage": final_stage,
            "channel_order": channel_order,
            "rerank_input_chunk_ids": (
                [row.chunk_id for row in ranked if row.stage == "fused"]
                if final_stage == "reranked"
                else None
            ),
        }
        self._artifacts.publish(output_directory, {
            f"{unit.variant_fingerprint}-{content_digest(unit.case_id)}-execution.json": _json_bytes(case_payload)})
        return QasperCaseResult(manifest_digest=manifest.digest, run_id=run_id, attempt_id=f"{run_id}:{unit.case_id}",
            unit=unit, query=case.query, paper_ids=(case.paper_id,), status=status, mapping_result=mapping_result,
            final_stage=final_stage, raw_ranked_results=ranked, metrics=metrics, failures=failure_records, reason=reason)

    def _report(
        self, manifest, variant_fingerprint, run_id, cases, experiment: ExperimentCase
    ):
        definitions = manifest.metric_definitions
        counts = {
            key: sum(case.status == key for case in cases)
            for key in ("ok", "needs_review", "system_failure", "not_applicable")
        }
        denominators = QasperDenominators(
            planned=len(cases),
            valid=counts["ok"],
            needs_review=counts["needs_review"],
            system_failure=counts["system_failure"],
            not_applicable=counts["not_applicable"],
        )
        aggregates = []
        for definition in definitions:
            valid = [
                metric
                for case in cases
                for metric in case.metrics
                if metric.key == definition.key and metric.status == "ok"
            ]
            if not valid:
                aggregates.append(
                    QasperMetric(
                        metric_id=definition.metric_id,
                        version=definition.version,
                        k=definition.k,
                        value=None,
                        status="system_failure",
                        numerator=None,
                        denominator=None,
                        reason="No valid cases in aggregate cohort.",
                        aggregation="macro_average",
                    )
                )
            else:
                # QASPER report metrics are a macro average over per-case values.
                # Per-question evidence-group denominators remain in case metrics.
                numerator = __import__("math").fsum(
                    float(metric.value) for metric in valid if metric.value is not None
                )
                denominator = len(valid)
                aggregates.append(
                    QasperMetric(
                        metric_id=definition.metric_id,
                        version=definition.version,
                        k=definition.k,
                        value=numerator / denominator,
                        status="ok",
                        numerator=numerator,
                        denominator=denominator,
                        reason=None,
                        aggregation="macro_average",
                    )
                )
        cases_payload = [case.model_dump(mode="json") for case in cases]
        metrics_payload = [metric.model_dump(mode="json") for metric in aggregates]
        failures_payload = [
            failure.model_dump(mode="json")
            for case in cases
            for failure in case.failures
        ]
        report_markdown = _variant_report_markdown(
            run_id, experiment, denominators, aggregates, cases
        )
        payloads = {
            "manifest": _json_bytes(manifest.model_dump(mode="json")),
            "cases": _json_bytes(cases_payload),
            "metrics": _json_bytes(metrics_payload),
            "failures": _json_bytes(failures_payload),
            "report": report_markdown,
        }
        refs = {
            name: _artifact_ref(name, _sha256_bytes(payload))
            for name, payload in payloads.items()
        }
        return QasperReportArtifact(
            manifest=manifest,
            run_id=run_id,
            selected_variant_fingerprint=variant_fingerprint,
            case_results=tuple(cases),
            denominators=denominators,
            metrics=tuple(aggregates),
            artifacts=QasperReportFiles(**refs),
        )

    def _publish_report(
        self,
        report: QasperReportArtifact,
        profile: str,
        experiment: ExperimentCase,
        *,
        directory: str,
    ) -> None:
        payloads = _variant_artifact_payloads(report, experiment)
        objects = {
            f"{profile}-manifest.json": payloads["manifest"],
            f"{profile}-cases.json": payloads["cases"],
            f"{profile}-metrics.json": payloads["metrics"],
            f"{profile}-failures.json": payloads["failures"],
            f"{profile}-report.md": payloads["report"],
            f"{profile}-report.json": _json_bytes(report.model_dump(mode="json")),
        }
        self._artifacts.publish(directory, objects)
        for name, data in objects.items():
            persisted = self._artifacts.read(directory, name)
            if persisted != data:
                raise ValueError(
                    f"published immutable artifact failed read-after-write verification: {name}"
                )
        _verify_report_artifacts(self._artifacts, directory, profile, report)


class _OfflinePolicy:
    multi_query_enabled = True


class _CaseFailure(RuntimeError):
    def __init__(self, status: str, stage: str, reason: str):
        self.status, self.stage, self.reason = status, stage, reason


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _artifact_ref(name: str, digest: str) -> ArtifactReference:
    return ArtifactReference(
        reference_id=f"qasper/{name}/{digest}",
        kind=f"qasper_{name}",
        uri=f"sha256://{digest}",
        content_sha256=digest,
    )


def _unmeasured(
    definitions: Sequence[QasperMetricDefinition], status: str, reason: str
) -> tuple[QasperMetric, ...]:
    return tuple(
        QasperMetric(
            metric_id=item.metric_id,
            version=item.version,
            k=item.k,
            value=None,
            status=status,
            numerator=None,
            denominator=None,
            reason=reason,
        )
        for item in definitions
    )


def _ranked_rows(
    result: Mapping[str, object], *, reranked: bool
) -> tuple[RankedResult, ...]:
    raw = result["raw_channel_results"]
    by_chunk: dict[str, list[QasperChannelProvenance]] = defaultdict(list)
    for row in raw:
        by_chunk[str(row["chunk_id"])].append(
            QasperChannelProvenance(
                channel={
                    "semantic": "dense",
                    "bm25": "bm25",
                    "field": "field",
                }[str(row["channel"])],
                query_id=str(row["query_id"]),
                query_sha256=str(row["query_hash"]),
                channel_rank=int(row["rank"]),
                raw_score=float(row["score"]),
                field=str(row["field_tag"]) if row.get("field_tag") else None,
            )
        )
    candidate_values = [
        candidate
        for group in result["channel_candidates"].values()
        for candidate in group
    ]
    if reranked:
        # Only the capped reranker input belongs to the fused execution stage.
        fused_values = result["fused_results"][
            : int(result["candidate_count_before_rerank"])
        ]
    else:
        # final_results is the candidate_limit-capped user-visible fused stage.
        fused_values = result["final_results"]
    stage_values: list[tuple[RetrievalStage, object]] = [
        ("candidate", candidate_values),
        ("fused", fused_values),
    ]
    if reranked:
        stage_values.append(("reranked", result["final_results"]))

    rows: list[RankedResult] = []
    for stage, values in stage_values:
        seen: set[str] = set()
        stage_rank = 0
        for chunk, score in values:
            cid = chunk.chunk_id
            if cid in seen:
                continue
            seen.add(cid)
            stage_rank += 1
            provenance = by_chunk.get(cid)
            if not provenance:
                raise ValueError(
                    f"retrieval result {cid!r} has no channel provenance"
                )
            rows.append(
                RankedResult(
                    chunk_id=cid,
                    rank=stage_rank,
                    stage=stage,
                    raw_score=float(score),
                    provenance=tuple(provenance),
                )
            )
    return tuple(rows)


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _load_run_index(
    store: QasperArtifactStore, directory: str
) -> tuple[str, ...]:
    data = json.loads(store.read(directory, "run-index.json"))
    profiles = data.get("profiles")
    if (
        not isinstance(profiles, list)
        or not profiles
        or any(not isinstance(item, str) for item in profiles)
        or len(profiles) != len(set(profiles))
    ):
        raise ValueError("run index is invalid")
    return tuple(profiles)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not __import__("math").isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _lexical_rerank(query: str, chunk) -> float:
    from backend.research.rag.retrieval.paper_lightweight_reranker import (
        LightweightLexicalReranker,
    )

    return LightweightLexicalReranker().score(query, [chunk.content])[0]


def _build_experiments(profiles, config: QasperEvaluationConfig, identity):
    if not config.rerank_enabled:
        raise ValueError("rerank_enabled must contain at least one explicit value")
    if not config.rerank_score_thresholds:
        raise ValueError("rerank_score_thresholds must contain an explicit value")
    if any(type(value) is not bool for value in config.rerank_enabled):
        raise ValueError("rerank_enabled values must be booleans")
    enabled_thresholds = tuple(
        dict.fromkeys(
            _finite_number(value, "rerank threshold")
            for value in config.rerank_score_thresholds
            if value is not None
        )
    )
    cases = []
    for profile in profiles:
        for limit in config.candidate_limits:
            for overfetch in config.overfetch_multipliers:
                for rrf_k in config.rrf_ks:
                    for weights in config.channel_weights:
                        for rerank in dict.fromkeys(config.rerank_enabled):
                            thresholds = enabled_thresholds if rerank else (None,)
                            if rerank and not thresholds:
                                raise ValueError(
                                    "enabled reranking requires at least one numeric threshold"
                                )
                            for threshold in thresholds:
                                cases.append(
                                    build_sweep(
                                        (profile,),
                                        candidate_limits=(limit,),
                                        overfetch_multipliers=(overfetch,),
                                        rrf_ks=(rrf_k,),
                                        rerank_score_thresholds=(threshold,),
                                        channel_weights=(weights,),
                                        identity=identity,
                                    )[0]
                                )
    fingerprints = [item.fingerprint for item in cases]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("sweep produced duplicate experiment identities")
    return tuple(cases)


def _current_corpus_sha256(documents: Sequence[object]) -> str:
    by_paper = {document.paper_id: document for document in documents}
    if len(by_paper) != len(documents):
        raise ValueError("current QASPER documents must have unique paper IDs")
    rows = []
    for paper_id, document in sorted(by_paper.items()):
        fulltext = "\n\n".join(
            section.text.strip()
            for section in document.sections
            if section.text.strip()
        )
        if not fulltext:
            raise ValueError(f"current QASPER document {paper_id!r} has no text")
        rows.append(
            {
                "paper_id": paper_id,
                "source_sha256": document.source_hash,
                "normalized_text_sha256": text_sha256(fulltext),
            }
        )
    return content_digest(rows)


def _validate_frozen_assets(frozen: FrozenQasperDataset, assets: object) -> None:
    dataset_digest = content_digest(assets.raw_dataset)
    if dataset_digest != frozen.dataset_digest:
        raise ValueError(
            "frozen mapping dataset content differs from current QASPER assets"
        )
    index_sha = calculate_index_content_sha256(assets.chunks)
    if (
        index_sha != frozen.index_sha256
        or index_sha != frozen.binding.index_content_sha256
        or frozen.binding.index_snapshot_id != f"qasper-index-{index_sha[:20]}"
    ):
        raise ValueError(
            "frozen mapping index content differs from current QASPER chunks"
        )
    corpus_sha = _current_corpus_sha256(assets.documents)
    if (
        corpus_sha != frozen.corpus_sha256
        or frozen.binding.corpus_snapshot_id != f"qasper-corpus-{corpus_sha[:20]}"
    ):
        raise ValueError(
            "frozen mapping corpus identity differs from current QASPER documents"
        )


def _metric_label(metric: QasperMetric) -> str:
    return metric.metric_id + (f"@{metric.k}" if metric.k is not None else "")


def _variant_report_markdown(
    run_id: str,
    experiment: ExperimentCase,
    denominators: QasperDenominators,
    metrics: Sequence[QasperMetric],
    cases: Sequence[QasperCaseResult],
) -> bytes:
    parameters = experiment.parameters.as_dict()
    lines = [
        "# QASPER Retrieval Evaluation",
        "",
        f"- Run ID: `{run_id}`",
        f"- Experiment ID: `{experiment.fingerprint}`",
        f"- Profile: `{experiment.profile.name}`",
        f"- Execution profile: `fixture` (not production latency)",
        f"- Parameters: `{json.dumps(parameters, sort_keys=True)}`",
        f"- Planned / valid: {denominators.planned} / {denominators.valid}",
        "- Aggregation: macro average of per-case metric values; per-case evidence denominators remain in cases.json.",
        "",
        "## Metrics",
        "",
        "| Metric | Value | Valid cases |",
        "|---|---:|---:|",
    ]
    for metric in metrics:
        value = "not measured" if metric.value is None else f"{metric.value:.6f}"
        denominator = "-" if metric.denominator is None else str(metric.denominator)
        lines.append(f"| {_metric_label(metric)} | {value} | {denominator} |")
    counts = _failure_counts(cases)
    lines.extend(["", "## Failure Attribution", ""])
    if counts:
        lines.extend(f"- `{key}`: {value}" for key, value in counts.items())
    else:
        lines.append("- No measured retrieval misses or execution failures.")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _variant_artifact_payloads(
    report: QasperReportArtifact, experiment: ExperimentCase
) -> dict[str, bytes]:
    return {
        "manifest": _json_bytes(report.manifest.model_dump(mode="json")),
        "cases": _json_bytes(
            [case.model_dump(mode="json") for case in report.case_results]
        ),
        "metrics": _json_bytes(
            [metric.model_dump(mode="json") for metric in report.metrics]
        ),
        "failures": _json_bytes(
            [
                failure.model_dump(mode="json")
                for case in report.case_results
                for failure in case.failures
            ]
        ),
        "report": _variant_report_markdown(
            report.run_id,
            experiment,
            report.denominators,
            report.metrics,
            report.case_results,
        ),
    }


def _verify_report_artifacts(
    store: QasperArtifactStore,
    directory: str,
    profile: str,
    report: QasperReportArtifact,
) -> None:
    names = {
        "manifest": f"{profile}-manifest.json",
        "cases": f"{profile}-cases.json",
        "metrics": f"{profile}-metrics.json",
        "failures": f"{profile}-failures.json",
        "report": f"{profile}-report.md",
    }
    for field, filename in names.items():
        payload = store.read(directory, filename)
        reference = getattr(report.artifacts, field)
        digest = _sha256_bytes(payload)
        if digest != reference.content_sha256:
            raise ValueError(
                f"artifact reference hash differs from actual persisted bytes: {filename}"
            )


def _run_report_markdown(summary: Mapping[str, object]) -> bytes:
    lines = [
        "# QASPER Evaluation Run Report",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Execution profile: `{summary['execution_profile']}`",
        f"- Latency semantics: {summary['latency_semantics']}",
        "",
        "## Experiments",
        "",
    ]
    for experiment in summary["experiments"]:
        lines.extend(
            [
                f"### {experiment['profile']}",
                "",
                f"- Experiment ID: `{experiment['experiment_id']}`",
                f"- Parameters: `{json.dumps(experiment['parameters'], sort_keys=True)}`",
                f"- Planned / valid: {experiment['denominators']['planned']} / {experiment['denominators']['valid']}",
                "- Diagnostic slices: question type, paper, section, field, evidence count, mapping status, and first-hit rank.",
                "- Question types: explicit dataset labels first; unknown values use deterministic derived-not-gold rules (`qasper-question-type-rules/v1`).",
                "",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _failure_counts(cases):
    counts = defaultdict(int)
    for case in cases:
        for failure in case.failures:
            counts[f"{failure.category}:{failure.stage}"] += 1
    return dict(sorted(counts.items()))
