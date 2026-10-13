from __future__ import annotations

from framework.shared import process_identity as subject


def test_windows_process_identity_probe_never_calls_os_kill(monkeypatch) -> None:
    monkeypatch.setattr(subject.sys, "platform", "win32")
    monkeypatch.setattr(
        subject,
        "_windows_process_identity",
        lambda pid: subject.ProcessIdentity(pid=pid, creation_id="windows-filetime:42"),
    )
    monkeypatch.setattr(
        subject.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("os.kill must not probe Windows processes")
        ),
    )

    assert subject.process_identity(1234) == subject.ProcessIdentity(
        pid=1234, creation_id="windows-filetime:42"
    )


def test_process_match_rejects_pid_reuse(monkeypatch) -> None:
    monkeypatch.setattr(
        subject,
        "process_identity",
        lambda pid: subject.ProcessIdentity(pid=pid, creation_id="procfs-starttime:new"),
    )

    assert subject.process_matches(1234, "procfs-starttime:old") is False
    assert subject.process_matches(1234, "procfs-starttime:new") is True
