"""local_json.save, artifact.*, and report.export / publish / search under faults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from backend.layers.output import tools as report_tools
from framework.tool import ToolStatus
from infrastructure.tools import local_json_tools
from tests.tools.harness.fault_stores import Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.invoke import expect_succeeded
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.test_notification_webhook_tools import outcome

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT = {
    "title": "Agent memory weekly",
    "sections": [{"title": "Findings", "content": "Memory retrieval matters."}],
    "source_urls": ["https://example.com/source"],
}


def _record(environment: ToolEnvironment, collection: str = "briefs", record_id: str = "r1") -> Path:
    return environment.local_json_root / collection / f"{record_id}.json"


def _save(environment: ToolEnvironment, **args: Any):
    arguments = {"collection": "briefs", "record_id": "r1", "value": {"version": 1}, **args}
    return environment.run("local_json.save", arguments)


@pytest.mark.tool_scenario("local_json.save", "A")
@pytest.mark.tool_scenario("local_json.save", "G")
@pytest.mark.parametrize(
    "args",
    [
        {"record_id": ""},
        {"record_id": "."},
        {"record_id": "../escape"},
        {"record_id": "a/b"},
        {"record_id": "a\\b"},
        {"record_id": "NUL"},
        {"record_id": "con"},
        {"record_id": "COM1"},
        {"record_id": "trailing."},
        {"record_id": "x" * 121},
        {"record_id": "名字"},
        {"collection": "."},
        {"collection": ".."},
        {"collection": "AUX"},
        {"metadata": ["not", "an", "object"]},
    ],
    ids=["empty", "dot", "traversal", "slash", "backslash", "nul", "con", "com1", "trailing-dot",
         "too-long", "non-ascii", "collection-dot", "collection-dotdot", "collection-aux", "metadata-array"],
)
def test_unsafe_names_never_touch_the_filesystem(tool_env: ToolEnvironment, args: dict) -> None:
    run = _save(tool_env, **args)

    assert outcome(run) == "not_applied", run.describe()
    assert not tool_env.local_json_root.exists() or not any(tool_env.local_json_root.rglob("*.json"))


@pytest.mark.tool_scenario("local_json.save", "A")
@pytest.mark.parametrize("value", [{"x": float("nan")}, [float("inf")], {"when": {1, 2}}], ids=["nan", "inf", "set"])
def test_values_that_are_not_json_are_refused(tool_env: ToolEnvironment, value: Any) -> None:
    assert outcome(_save(tool_env, value=value)) == "not_applied"
    assert not _record(tool_env).exists()


@pytest.mark.tool_scenario("local_json.save", "F")
def test_a_crash_mid_write_keeps_the_previous_record(tool_env: ToolEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    expect_succeeded(_save(tool_env, value={"version": 1}))
    before = _record(tool_env).read_bytes()

    def disk_full_while_writing(path: Any, content: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(local_json_tools, "write_bytes_atomically_unlocked", disk_full_while_writing)
    run = _save(tool_env, value={"version": 2})

    assert outcome(run) == "not_applied"
    assert "left unchanged" in run.error_message
    assert _record(tool_env).read_bytes() == before


@pytest.mark.tool_scenario("local_json.save", "F")
def test_interrupted_atomic_write_never_exposes_a_partial_record(
    tool_env: ToolEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the real writer and fail its rename, so the half-written temp file exists."""

    from infrastructure.storage import json_file_store

    expect_succeeded(_save(tool_env, value={"version": 1}))
    before = _record(tool_env).read_bytes()

    def rename_fails(temporary: Path, resolved: Path) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(json_file_store, "_replace_with_retry", rename_fails)
    run = _save(tool_env, value={"version": 2, "padding": "p" * 100_000})

    assert outcome(run) == "not_applied"
    assert _record(tool_env).read_bytes() == before
    assert [path.name for path in _record(tool_env).parent.glob("*.tmp")] == []


@pytest.mark.tool_scenario("local_json.save", "B")
def test_unwritable_root_is_reported_without_a_partial_file(tool_env: ToolEnvironment) -> None:
    tool_env.local_json_root.parent.mkdir(parents=True, exist_ok=True)
    tool_env.local_json_root.write_text("not a directory", encoding="utf-8")

    run = _save(tool_env)

    assert outcome(run) == "not_applied"
    assert tool_env.local_json_root.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.tool_scenario("local_json.save", "E")
def test_concurrent_writers_to_one_record_leave_one_whole_version(tool_env: ToolEnvironment) -> None:
    def save(index: int) -> None:
        expect_succeeded(_save(tool_env, value={"writer": index, "padding": "x" * 50_000}))

    threads = [threading.Thread(target=save, args=(index,)) for index in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    record = json.loads(_record(tool_env).read_text(encoding="utf-8"))
    assert record["value"]["writer"] in range(30)
    assert len(record["value"]["padding"]) == 50_000
    assert sorted(path.name for path in _record(tool_env).parent.iterdir()) == ["r1.json", "r1.json.lock"]


_WRITER_SCRIPT = """
import sys
from infrastructure.tools.local_json_tools import LocalJsonToolStore

store = LocalJsonToolStore(sys.argv[1])
for index in range(int(sys.argv[3])):
    store.save(collection="briefs", record_id="hot", value={"writer": sys.argv[2], "index": index, "pad": "y" * 40000}, metadata={})
"""


@pytest.mark.tool_scenario("local_json.save", "E")
def test_concurrent_writer_processes_never_leave_a_torn_record(tool_env: ToolEnvironment) -> None:
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER_SCRIPT, str(tool_env.local_json_root), name, "25"],
            cwd=REPO_ROOT,
            env=environment,
        )
        for name in ("alpha", "beta")
    ]
    assert [worker.wait(timeout=120) for worker in workers] == [0, 0]

    record = json.loads(_record(tool_env, record_id="hot").read_text(encoding="utf-8"))
    assert record["value"]["writer"] in {"alpha", "beta"} and len(record["value"]["pad"]) == 40_000


@pytest.mark.tool_scenario("local_json.save", "D")
def test_large_value_is_written_whole(tool_env: ToolEnvironment) -> None:
    value = {"items": [{"id": index, "text": "z" * 100} for index in range(5_000)]}
    expect_succeeded(_save(tool_env, value=value))
    assert json.loads(_record(tool_env).read_text(encoding="utf-8"))["value"] == value


def _run_dir(environment: ToolEnvironment) -> Path:
    path = Path(environment.artifact_manager.run_dir(environment.identity.run_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.tool_scenario("artifact.write", "A")
@pytest.mark.tool_scenario("artifact.write", "G")
@pytest.mark.parametrize(
    "path",
    ["../escape.txt", "/etc/passwd", "C:/Windows/win.ini", "a/../../b.txt", "notes/NUL.txt", "trailing./x.txt", "", "a//b"],
)
def test_artifact_paths_cannot_leave_the_run(tool_env: ToolEnvironment, path: str) -> None:
    run = tool_env.run("artifact.write", {"path": path, "content": "x"})

    assert outcome(run) == "not_applied", run.describe()
    assert list(_run_dir(tool_env).rglob("*")) == []


@pytest.mark.tool_scenario("artifact.write", "A")
def test_json_artifacts_refuse_values_strict_readers_reject(tool_env: ToolEnvironment) -> None:
    run = tool_env.run("artifact.write", {"path": "data.json", "content": {"score": float("nan")}})
    assert outcome(run) == "not_applied"
    assert not (_run_dir(tool_env) / "data.json").exists()


@pytest.mark.tool_scenario("artifact.write", "B")
@pytest.mark.tool_scenario("artifact.write", "F")
def test_artifact_write_failure_keeps_the_previous_version(
    tool_env: ToolEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framework.agent.artifacts.runtime import manager

    expect_succeeded(tool_env.run("artifact.write", {"path": "notes.txt", "content": "first"}))

    def disk_full(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manager, "verified_atomic_write", disk_full)
    run = tool_env.run("artifact.write", {"path": "notes.txt", "content": "second"})

    assert run.status is ToolStatus.FAILED
    assert (_run_dir(tool_env) / "notes.txt").read_text(encoding="utf-8") == "first"


@pytest.mark.tool_scenario("artifact.write", "D")
def test_large_artifact_round_trips(tool_env: ToolEnvironment) -> None:
    content = "line\n" * 150_000
    expect_succeeded(tool_env.run("artifact.write", {"path": "big.txt", "content": content}))
    assert (_run_dir(tool_env) / "big.txt").read_text(encoding="utf-8") == content


@pytest.mark.tool_scenario("artifact.write", "E")
def test_concurrent_writes_to_one_artifact_leave_one_whole_version(tool_env: ToolEnvironment) -> None:
    def write(index: int) -> None:
        expect_succeeded(tool_env.run("artifact.write", {"path": "shared.txt", "content": f"{index}:" + "w" * 40_000}))

    threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    text = (_run_dir(tool_env) / "shared.txt").read_text(encoding="utf-8")
    head, _, body = text.partition(":")
    assert int(head) in range(20) and body == "w" * 40_000


@pytest.mark.tool_scenario("artifact.load", "A")
@pytest.mark.tool_scenario("artifact.load", "G")
@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "", "a/../../x"])
def test_artifact_load_cannot_read_outside_the_run(tool_env: ToolEnvironment, path: str) -> None:
    (Path(tool_env.artifact_manager.root) / "outside.txt").parent.mkdir(parents=True, exist_ok=True)
    (Path(tool_env.artifact_manager.root) / "outside.txt").write_text("other run", encoding="utf-8")
    run = tool_env.run("artifact.load", {"path": path})
    assert run.status is ToolStatus.FAILED
    assert "other run" not in json.dumps(run.output, default=str)


@pytest.mark.tool_scenario("artifact.load", "B")
@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("missing.txt", None, "artifact not found: missing.txt"),
        ("blob.txt", b"\xff\xfe\x00binary", "not UTF-8 text"),
        ("broken.json", b'{"a": 1', "not valid JSON"),
    ],
    ids=["missing", "binary", "broken-json"],
)
def test_unreadable_artifacts_fail_with_a_run_relative_message(
    tool_env: ToolEnvironment, name: str, content: bytes | None, message: str
) -> None:
    if content is not None:
        (_run_dir(tool_env) / name).write_bytes(content)

    run = tool_env.run("artifact.load", {"path": name})

    assert run.status is ToolStatus.FAILED
    assert message in run.error_message
    assert str(tool_env.root) not in run.error_message


@pytest.mark.tool_scenario("artifact.load", "D")
def test_artifact_too_large_to_return_is_refused_before_reading(tool_env: ToolEnvironment) -> None:
    from framework.tool.builtin import artifact

    (_run_dir(tool_env) / "huge.txt").write_bytes(b"h" * (artifact.MAX_LOAD_BYTES + 1))
    run = tool_env.run("artifact.load", {"path": "huge.txt"})
    assert run.status is ToolStatus.FAILED
    assert "reads at most" in run.error_message


@pytest.mark.tool_scenario("artifact.search", "A")
@pytest.mark.tool_scenario("artifact.search", "G")
@pytest.mark.parametrize("args", [{"path_prefix": "../"}, {"path_prefix": "/"}, {"max_results": "many"}])
def test_artifact_search_rejects_unusable_arguments(tool_env: ToolEnvironment, args: dict) -> None:
    assert tool_env.run("artifact.search", args).status is ToolStatus.FAILED


@pytest.mark.tool_scenario("artifact.search", "B")
def test_artifact_search_skips_binary_and_matches_text(tool_env: ToolEnvironment) -> None:
    run_dir = _run_dir(tool_env)
    (run_dir / "notes.md").write_text("agent memory notes", encoding="utf-8")
    (run_dir / "image.png").write_bytes(b"\x89PNG agent memory")
    (run_dir / "garbled.txt").write_bytes(b"\xff\xfe agent memory")

    output = expect_succeeded(tool_env.run("artifact.search", {"query": "agent memory"}))

    assert sorted(match["path"] for match in output["artifacts"]) == ["garbled.txt", "notes.md"]


@pytest.mark.tool_scenario("artifact.search", "D")
def test_artifact_search_over_many_files_is_bounded(tool_env: ToolEnvironment) -> None:
    run_dir = _run_dir(tool_env)
    for index in range(600):
        (run_dir / f"doc-{index}.txt").write_text("needle " + "hay " * 200, encoding="utf-8")
    (run_dir / "big.txt").write_bytes(b"h" * 3_000_000 + b" needle")

    run = tool_env.run("artifact.search", {"query": "needle", "max_results": 1000})

    output = expect_succeeded(run)
    assert output["match_count"] == 100
    assert run.elapsed_seconds < 10.0


def _publish(environment: ToolEnvironment, **args: Any):
    return environment.run("report.publish", {"report": REPORT, **args})


@pytest.mark.tool_scenario("report.export", "A")
@pytest.mark.tool_scenario("report.publish", "A")
@pytest.mark.parametrize("tool", ["report.export", "report.publish"])
@pytest.mark.parametrize(
    "report",
    [
        {"sections": []},
        {"title": "t", "sections": ["not an object"]},
        {"title": "t", "sections": [{"content": "no title"}]},
        {"title": "t", "sections": "not a list"},
        {"title": "t", "metadata": {"score": float("nan")}},
    ],
    ids=["no-title", "section-string", "section-untitled", "sections-string", "nan-metadata"],
)
def test_unusable_reports_are_not_written(tool_env: ToolEnvironment, tool: str, report: dict) -> None:
    run = tool_env.run(tool, {"report": report})

    assert outcome(run) == "not_applied", run.describe()
    assert tool_env.reports.plan.calls("save_report") == 0


@pytest.mark.tool_scenario("report.export", "A")
def test_section_without_content_renders_instead_of_crashing(tool_env: ToolEnvironment) -> None:
    output = expect_succeeded(tool_env.run("report.export", {"report": {"title": "Brief", "sections": [{"title": "Empty"}]}}))
    assert (_run_dir(tool_env) / output["relative_path"]).read_text(encoding="utf-8").startswith("# Brief")


@pytest.mark.tool_scenario("report.export", "G")
@pytest.mark.parametrize("path", ["../escape.md", "/tmp/x.md", "reports/NUL.md"])
def test_export_path_cannot_leave_the_run(tool_env: ToolEnvironment, path: str) -> None:
    assert outcome(tool_env.run("report.export", {"report": REPORT, "path": path})) == "not_applied"


@pytest.mark.tool_scenario("report.export", "B")
@pytest.mark.tool_scenario("report.export", "F")
def test_export_failure_keeps_the_previous_file(tool_env: ToolEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    from framework.agent.artifacts.runtime import manager

    first = expect_succeeded(tool_env.run("report.export", {"report": REPORT}))
    target = _run_dir(tool_env) / first["relative_path"]
    before = target.read_bytes()

    def disk_full(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manager, "verified_atomic_write", disk_full)
    run = tool_env.run("report.export", {"report": {**REPORT, "sections": [{"title": "Changed"}]}})

    assert run.status is ToolStatus.FAILED
    assert target.read_bytes() == before


@pytest.mark.tool_scenario("report.export", "D")
def test_large_report_exports_whole(tool_env: ToolEnvironment) -> None:
    sections = [{"title": f"Section {index}", "content": "c" * 2_000} for index in range(300)]
    output = expect_succeeded(tool_env.run("report.export", {"report": {"title": "Big", "sections": sections}, "format": "json"}))
    exported = json.loads((_run_dir(tool_env) / output["relative_path"]).read_text(encoding="utf-8"))
    assert len(exported["sections"]) == 300


@pytest.mark.tool_scenario("report.publish", "A")
@pytest.mark.parametrize(
    "args",
    [{"quality_score": float("nan")}, {"quality_score": 1.5}, {"citation_coverage_score": -1}, {"run_id": "another-run"}],
    ids=["score-nan", "score-above-1", "coverage-negative", "foreign-run"],
)
def test_publish_refuses_unstorable_records(tool_env: ToolEnvironment, args: dict) -> None:
    assert outcome(_publish(tool_env, **args)) == "not_applied"
    assert tool_env.reports.plan.calls("save_report") == 0


@pytest.mark.tool_scenario("report.publish", "B")
def test_publish_store_failure_is_indeterminate(tool_env: ToolEnvironment) -> None:
    tool_env.reports.plan.script("save_report", Raise(ConnectionError("repository dropped the write")))
    assert outcome(_publish(tool_env)) == "indeterminate"


@pytest.mark.tool_scenario("report.publish", "C")
def test_publish_stall_ends_on_the_tool_timeout(tool_env_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_tools, "REPORT_STORE_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.reports.plan.script("save_report", Stall())
    try:
        run = _publish(environment)
        assert run.status is ToolStatus.TIMEOUT, run.describe()
    finally:
        environment.reports.plan.release()


@pytest.mark.tool_scenario("report.publish", "F")
def test_republishing_after_a_lost_acknowledgement_replaces_the_record(tool_env: ToolEnvironment) -> None:
    tool_env.reports.plan.script("save_report", RaiseAfter(TimeoutError("ack lost")), Pass())

    assert outcome(_publish(tool_env)) == "indeterminate"
    expect_succeeded(_publish(tool_env, report={**REPORT, "title": "Agent memory weekly v2"}))

    assert list(tool_env.reports.reports) == [f"{tool_env.identity.run_id}:final"]
    assert tool_env.reports.reports[f"{tool_env.identity.run_id}:final"].title == "Agent memory weekly v2"


@pytest.mark.tool_scenario("report.publish", "G")
def test_publish_is_bound_to_its_own_run(tool_env: ToolEnvironment) -> None:
    from framework.tool import ToolExecutor
    from tests.tools.harness.invoke import run_tool
    from tests.tools.harness.registry import tool_test_identity

    other = tool_test_identity("run-someone-else")
    run = run_tool(tool_env.registry, "report.publish", {"report": REPORT}, executor=ToolExecutor(tool_env.registry, graph_identity=other))

    assert run.status is not ToolStatus.SUCCEEDED
    assert tool_env.reports.plan.calls("save_report") == 0


@pytest.mark.tool_scenario("report.search", "A")
@pytest.mark.parametrize("args", [{"query": ""}, {"query": "  "}, {"query": "x", "limit": "many"}])
def test_search_rejects_unusable_queries(tool_env: ToolEnvironment, args: dict) -> None:
    assert tool_env.run("report.search", args).status is ToolStatus.FAILED
    assert tool_env.reports.plan.calls("search_reports") == 0


@pytest.mark.tool_scenario("report.search", "B")
def test_search_store_failure_is_a_clean_read_failure(tool_env: ToolEnvironment) -> None:
    tool_env.reports.plan.script("search_reports", Raise(ConnectionError("report store down")))
    run = tool_env.run("report.search", {"query": "memory"})
    assert run.status is ToolStatus.FAILED and not run.observation.result.indeterminate


@pytest.mark.tool_scenario("report.search", "C")
def test_search_stall_ends_on_the_tool_timeout(tool_env_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report_tools, "REPORT_STORE_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.reports.plan.script("search_reports", Stall())
    try:
        assert environment.run("report.search", {"query": "memory"}).status is ToolStatus.TIMEOUT
    finally:
        environment.reports.plan.release()


@pytest.mark.tool_scenario("report.search", "E")
def test_parallel_searches_overlap(tool_env: ToolEnvironment) -> None:
    tool_env.reports.plan.script("search_reports", Stall(seconds=0.2))
    observations, elapsed = tool_env.run_batch([("report.search", {"query": f"q{index}"}) for index in range(10)])
    assert all(observation.status is ToolStatus.SUCCEEDED for observation in observations)
    assert elapsed < 1.2
