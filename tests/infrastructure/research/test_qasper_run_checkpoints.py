from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.research.qasper_evaluation import FilesystemQasperRunCheckpoints


def test_checkpoint_lives_beside_the_output_and_round_trips_lines(tmp_path: Path) -> None:
    store = FilesystemQasperRunCheckpoints()
    output = str(tmp_path / "run")

    assert store.read_lines(output) == ()
    store.append_line(output, b'{"n":1}')
    store.append_line(output, b'{"n":2}')

    assert (tmp_path / "run.checkpoint.jsonl").is_file()
    assert not (tmp_path / "run").exists()
    assert store.read_lines(output) == (b'{"n":1}', b'{"n":2}')
    store.discard(output)
    assert store.read_lines(output) == ()


def test_torn_trailing_line_is_ignored_and_truncated_by_the_next_append(tmp_path: Path) -> None:
    store = FilesystemQasperRunCheckpoints()
    output = str(tmp_path / "run")
    store.append_line(output, b'{"n":1}')
    with (tmp_path / "run.checkpoint.jsonl").open("ab") as stream:
        stream.write(b'{"n":2, "interrupted')

    assert store.read_lines(output) == (b'{"n":1}',)
    store.append_line(output, b'{"n":3}')

    assert (tmp_path / "run.checkpoint.jsonl").read_bytes() == b'{"n":1}\n{"n":3}\n'


@pytest.mark.parametrize("line", [b"", b"a\nb", "text"])
def test_append_rejects_lines_that_would_break_the_log(tmp_path: Path, line) -> None:
    with pytest.raises(ValueError):
        FilesystemQasperRunCheckpoints().append_line(str(tmp_path / "run"), line)
