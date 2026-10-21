from __future__ import annotations

from types import MappingProxyType

import pytest

from backend.research.application.catalog import InMemoryResearchCatalogRepository
from backend.research.application.parse_paper import (
    ParsePaperError,
    ParsePaperRequest,
    ParsePaperUseCase,
    _known_document_parser_configuration,
    _known_source_fetcher_configuration,
    _parse_cache_fingerprint,
)
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.latex_compiler import ArxivLatexDocumentCompiler
from tests.backend.research.application.test_parse_paper_catalog_contract import _Parser, _Resolver


class _ConfiguredParser:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.calls = 0

    def cache_identity(self):
        return {"version": "1", "profile": self.profile}

    def parse(self, paper_id, source_bytes):
        self.calls += 1
        document = _Parser().parse(paper_id, source_bytes)
        return document.model_copy(update={"metadata": {**document.metadata, "profile": self.profile}})


class _OpaqueParser:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.calls = 0

    def parse(self, paper_id, source_bytes):
        self.calls += 1
        return _Parser().parse(paper_id, source_bytes)


class _ConfiguredFetcher:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def cache_identity(self):
        return {"version": "1", "endpoint": self.endpoint}


class _OpaqueFetcher:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint


def _fingerprint(parser, *, fetcher=None):
    compiler = ArxivLatexDocumentCompiler(fetcher) if fetcher is not None else None
    return _parse_cache_fingerprint(
        parser, compiler, PaperDocumentChunker(), {}, "text/html", source_hash="source-content-hash"
    )


def _use_case(repository, parser, *, compiler=None):
    return ParsePaperUseCase(
        source_resolver=_Resolver(source_url="https://publisher.example/cache", paper_id="cache-paper", content=b"cache-source"),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        document_parser=parser,
        document_compiler=compiler,
        chunker=PaperDocumentChunker(),
    )


def _request(run_id):
    return ParsePaperRequest(source="https://publisher.example/cache", run_id=run_id, tenant_id="tenant", user_id="reader")


@pytest.mark.parametrize("role", ["parser", "fetcher"])
def test_explicit_instance_configuration_changes_cache_fingerprint(role):
    if role == "parser":
        first = _fingerprint(_ConfiguredParser("strict"))
        same = _fingerprint(_ConfiguredParser("strict"))
        different = _fingerprint(_ConfiguredParser("relaxed"))
    else:
        first = _fingerprint(_ConfiguredParser("strict"), fetcher=_ConfiguredFetcher("https://a.example"))
        same = _fingerprint(_ConfiguredParser("strict"), fetcher=_ConfiguredFetcher("https://a.example"))
        different = _fingerprint(_ConfiguredParser("strict"), fetcher=_ConfiguredFetcher("https://b.example"))
    assert first is not None
    assert first == same
    assert first != different


@pytest.mark.parametrize("role", ["parser", "fetcher"])
def test_opaque_external_configuration_is_not_cacheable(role):
    if role == "parser":
        assert _known_document_parser_configuration(_OpaqueParser("strict")) is None
        assert _fingerprint(_OpaqueParser("strict")) is None
    else:
        assert _known_source_fetcher_configuration(_OpaqueFetcher("https://a.example")) is None
        assert _fingerprint(_ConfiguredParser("strict"), fetcher=_OpaqueFetcher("https://a.example")) is None


@pytest.mark.parametrize("role", ["parser", "fetcher"])
@pytest.mark.parametrize("value", [{}, {"window": float("nan")}, {"window": object()}, {"window": "unknown"}])
def test_invalid_declared_identity_cannot_enable_cache(role, value):
    component = _ConfiguredParser("strict") if role == "parser" else _ConfiguredFetcher("https://a.example")
    component.cache_identity = lambda: value
    fingerprint = _fingerprint(component) if role == "parser" else _fingerprint(_ConfiguredParser("strict"), fetcher=component)
    assert fingerprint is None


@pytest.mark.parametrize("role", ["parser", "fetcher"])
def test_declared_identity_is_a_canonical_detached_mapping(role):
    declared = {"version": "1", "configuration": {"b": [2, 3], "a": 1}}
    component = _ConfiguredParser("strict") if role == "parser" else _ConfiguredFetcher("https://a.example")
    component.cache_identity = lambda: MappingProxyType(declared)
    helper = _known_document_parser_configuration if role == "parser" else _known_source_fetcher_configuration
    identity = helper(component)
    assert identity is not None
    assert identity["declared_identity"] == declared
    declared["configuration"]["b"].append(4)
    assert identity["declared_identity"]["configuration"]["b"] == [2, 3]


@pytest.mark.parametrize("role", ["parser", "fetcher"])
@pytest.mark.parametrize("same_configuration", [False, True])
def test_external_configuration_controls_real_document_cache_reuse(role, same_configuration):
    repository = InMemoryResearchCatalogRepository()
    first_parser = _ConfiguredParser("strict")
    second_parser = _ConfiguredParser("strict" if same_configuration or role == "fetcher" else "relaxed")
    first_compiler = ArxivLatexDocumentCompiler(_ConfiguredFetcher("https://a.example")) if role == "fetcher" else None
    second_compiler = ArxivLatexDocumentCompiler(_ConfiguredFetcher("https://a.example" if same_configuration else "https://b.example")) if role == "fetcher" else None
    first = _use_case(repository, first_parser, compiler=first_compiler).parse(_request("cache-first"))
    second_use_case = _use_case(repository, second_parser, compiler=second_compiler)
    if same_configuration:
        second = second_use_case.parse(_request("cache-second"))
        assert second.idempotent is True
        assert second.document == first.document
    else:
        with pytest.raises(ParsePaperError) as error:
            second_use_case.parse(_request("cache-second"))
        assert error.value.code == "parse_snapshot_activation_blocked"
    assert first_parser.calls == 1
    assert second_parser.calls == 0


@pytest.mark.parametrize("role", ["parser", "fetcher"])
def test_opaque_configuration_never_reads_or_admits_reusable_cache(role, monkeypatch):
    repository = InMemoryResearchCatalogRepository()
    parser = _OpaqueParser("strict") if role == "parser" else _ConfiguredParser("strict")
    compiler = ArxivLatexDocumentCompiler(_OpaqueFetcher("https://a.example")) if role == "fetcher" else None
    use_case = _use_case(repository, parser, compiler=compiler)

    def forbidden_lookup(*_args, **_kwargs):
        pytest.fail("opaque configuration must not attempt reusable document cache lookup")

    monkeypatch.setattr(use_case, "_existing_document", forbidden_lookup)
    result = use_case.parse(_request("opaque-first"))
    assert result.document is not None
    assert result.document.metadata.get("parse_cache_identity") is None
    assert result.document.metadata.get("parse_cache_fingerprint") is None
    assert result.document.source_hash
    assert parser.calls == 1
    with pytest.raises(ParsePaperError) as error:
        use_case.parse(_request("opaque-second"))
    assert error.value.code == "parse_snapshot_activation_blocked"
    assert parser.calls == 1


@pytest.mark.parametrize("role", ["parser", "fetcher"])
@pytest.mark.parametrize("hook", ["raises", "noncallable", "property_raises"])
def test_unavailable_declared_identity_never_falls_back_to_module_or_state(role, hook):
    base = _ConfiguredParser if role == "parser" else _ConfiguredFetcher

    class _BrokenIdentity(base):
        @property
        def cache_identity(self):
            if hook == "property_raises":
                raise RuntimeError("identity unavailable")
            if hook == "noncallable":
                return {"version": "1"}

            def unavailable():
                raise RuntimeError("identity unavailable")

            return unavailable

    component = _BrokenIdentity("configured")
    fingerprint = (
        _fingerprint(component) if role == "parser"
        else _fingerprint(_ConfiguredParser("strict"), fetcher=component)
    )
    assert fingerprint is None


@pytest.mark.parametrize("identity", [None, {}])
def test_unknown_cache_identity_is_rejected_before_direct_repository_cache_lookup(identity, monkeypatch):
    use_case = _use_case(InMemoryResearchCatalogRepository(), _OpaqueParser("strict"))
    request = _request("no-cache-read")
    snapshot = _Resolver(
        source_url=request.source, paper_id="cache-paper", content=b"cache-source",
    ).resolve(request).snapshot

    def forbidden_store_read(*_args, **_kwargs):
        pytest.fail("unknown identity must be rejected before reading reusable cache")

    monkeypatch.setattr(use_case, "_stored_document", forbidden_store_read)
    assert use_case._existing_document(
        "cache-paper", snapshot,
        parse_cache_identity=identity, parse_cache_fingerprint=None,
    ) is None


@pytest.mark.parametrize("role", ["parser", "fetcher"])
def test_opaque_configuration_cannot_reuse_or_overwrite_an_identified_snapshot(role):
    repository = InMemoryResearchCatalogRepository()
    first = _use_case(repository, _ConfiguredParser("strict")).parse(_request("known-first"))
    parser = _OpaqueParser("strict") if role == "parser" else _ConfiguredParser("strict")
    compiler = ArxivLatexDocumentCompiler(_OpaqueFetcher("https://a.example")) if role == "fetcher" else None
    use_case = _use_case(repository, parser, compiler=compiler)
    with pytest.raises(ParsePaperError) as captured:
        use_case.parse(_request("opaque-after-known"))
    assert captured.value.code == "parse_snapshot_activation_blocked"
    assert captured.value.details["cache_identity_available"] is False
    assert parser.calls == 0
    assert use_case._stored_document(
        "cache-paper", actor_scope={"tenant_id": "tenant", "user_id": "reader"},
    ) == first.document
