from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.desktop_app.protocol import MAX_STARTUP_CONTROL_BYTES, parse_startup_control_message
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.lifecycle_cancellation import (
    DesktopStartCancelled,
    check_startup_cancellation,
    parse_startup_control_message as owned_startup_control_parser,
    startup_cancellation_context,
)


def test_check_is_noop_without_cli_owned_context() -> None:
    check_startup_cancellation()


def test_private_control_is_canonical_single_bounded_message() -> None:
    assert parse_startup_control_message is owned_startup_control_parser
    assert parse_startup_control_message(b'{"command":"cancel-start"}\n') == "cancel-start"
    for invalid in (
        b'{ "command": "cancel-start" }\n',
        b'{"command":"cancel-start","command":"cancel-start"}\n',
        b'{"command":"cancel-start"}\n{"command":"cancel-start"}\n',
        b'{"command":"stop"}\n',
        b'x' * (MAX_STARTUP_CONTROL_BYTES + 1),
    ):
        with pytest.raises(DesktopOperationError) as raised:
            parse_startup_control_message(invalid)
        assert raised.value.reason_code == "desktop_start_control_invalid"


def test_context_turns_private_message_into_checkpoint_exception(capfd) -> None:
    with startup_cancellation_context(stream=io.BytesIO(b'{"command":"cancel-start"}\n')) as cancellation:
        assert cancellation.wait(1.0) is True
        with pytest.raises(DesktopStartCancelled) as raised:
            check_startup_cancellation()

    captured = capfd.readouterr()
    assert raised.value.reason_code == "desktop_start_cancelled"
    assert captured.out == ""
    assert captured.err == "desktop startup cancellation requested\n"


def test_invalid_control_diagnostic_does_not_reflect_secret(capfd) -> None:
    secret = "Bearer private-start-secret"
    with startup_cancellation_context(stream=io.BytesIO(f'{{"command":"{secret}"}}\n'.encode())) as cancellation:
        assert cancellation.wait(0.05) is False

    captured = capfd.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    assert captured.err == "desktop startup control message was rejected\n"


def test_no_cancel_with_parent_stdin_still_open_exits_without_buffered_stdio_fatal() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
from scripts.local_desktop.lifecycle_cancellation import startup_cancellation_context
with startup_cancellation_context():
    print('{"ok":true,"command":"start"}', flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", script],
        cwd=project_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and not process.stdin.closed
        return_code = process.wait(timeout=10)
        assert not process.stdin.closed
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = process.stdout.read()
        stderr = process.stderr.read()
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert return_code == 0
    assert stdout in (b'{"ok":true,"command":"start"}\n', b'{"ok":true,"command":"start"}\r\n')
    assert b"Fatal Python error" not in stderr
    assert b"_enter_buffered_busy" not in stderr
    assert stderr == b""


def test_safe_tree_import_does_not_load_desktop_app_composition() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = """
import sys
import scripts.local_desktop.safe_tree
assert 'scripts.desktop_app.runtime' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=project_root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
