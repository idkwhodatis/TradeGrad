"""Pure fake-Popen/waitid/procfs checks. Never execute or signal a real child."""

import os
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from script import ci_owned_group as groups


def stat_fields(pid, *, parent=100, group=None, session=None, state="S", start="300"):
    values = [
        state,
        str(parent),
        str(pid if group is None else group),
        str(pid if session is None else session),
    ] + ["0"] * 16
    values[19] = start
    return values


@pytest.fixture
def model(monkeypatch):
    calls = []
    data = {201: stat_fields(201)}
    state = SimpleNamespace(exit=None, now=0.0)
    process = SimpleNamespace(pid=201, returncode=None)

    def wait(timeout):
        calls.append(("wait", timeout))
        process.returncode = (
            state.exit.si_status
            if state.exit.si_code == os.CLD_EXITED
            else -state.exit.si_status
        )
        return process.returncode

    process.wait = wait
    monkeypatch.setattr(groups, "process_fields", lambda pid: data[pid])
    monkeypatch.setattr(groups.os, "getpid", lambda: 100)
    monkeypatch.setattr(groups.signal, "getsignal", lambda *_: signal.SIG_DFL)
    monkeypatch.setattr(
        groups.os, "pidfd_open", lambda pid: calls.append(("pin", pid)) or 44
    )
    monkeypatch.setattr(
        groups.os, "waitid", lambda *a: calls.append(("waitid", *a)) or state.exit
    )
    monkeypatch.setattr(groups.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(groups.os, "killpg", lambda *a: calls.append(("group", *a)))
    monkeypatch.setattr(
        groups.signal, "pidfd_send_signal", lambda *a: calls.append(("pidfd", *a))
    )
    monkeypatch.setattr(
        groups.Path, "iterdir", lambda *_: [Path(f"/proc/{pid}") for pid in data]
    )
    monkeypatch.setattr(groups.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(
        groups.time, "sleep", lambda seconds: setattr(state, "now", state.now + seconds)
    )
    return process, data, state, calls


def exited(state, data, *, killed=False):
    data[201][0] = "Z"
    state.exit = SimpleNamespace(
        si_code=os.CLD_KILLED if killed else os.CLD_EXITED,
        si_status=signal.SIGKILL if killed else 0,
    )


def test_admission_pins_exact_direct_child_and_preserves_unreaped_identity(model):
    process, _, _, calls = model
    groups.admit_owned_group(process)
    assert calls[:2] == [
        ("pin", 201),
        ("waitid", os.P_PIDFD, 44, os.WEXITED | os.WNOHANG | os.WNOWAIT),
    ]
    assert process.returncode is None
    assert process._tradegrad_group == {"fd": 44, "start": "300", "verified": False}


@pytest.mark.parametrize("field,value", [(1, "999"), (2, "999"), (3, "999")])
def test_foreign_parent_group_or_session_never_admitted(model, field, value):
    process, data, _, calls = model
    data[201][field] = value
    with pytest.raises(RuntimeError, match="Exclusive"):
        groups.admit_owned_group(process)
    assert calls[-1] == ("close", 44)
    assert not any(call[0] in {"group", "pidfd", "wait"} for call in calls)


def test_peek_reports_exit_without_reaping_or_releasing_pgid(model):
    process, data, state, calls = model
    groups.admit_owned_group(process)
    assert groups.peek_owned_group(process) is None
    exited(state, data)
    assert groups.peek_owned_group(process) == 0
    assert process.returncode is None
    assert not any(call[0] in {"close", "wait"} for call in calls)


def test_signal_uses_fresh_wnowait_identity_and_process_signal_uses_pidfd(model):
    process, _, _, calls = model
    groups.admit_owned_group(process)
    groups.signal_owned_group(process, signal.SIGSTOP)
    assert calls[-2:] == [
        ("waitid", os.P_PIDFD, 44, os.WEXITED | os.WNOHANG | os.WNOWAIT),
        ("group", 201, signal.SIGSTOP),
    ]
    groups.signal_owned_process(process, signal.SIGCONT)
    assert calls[-1] == ("pidfd", 44, signal.SIGCONT)


def test_reaped_root_never_signalled_even_if_same_pid_is_now_a_child(model):
    process, data, _, calls = model
    groups.admit_owned_group(process)
    process.returncode = 0
    data[201] = stat_fields(201, start="999")
    with pytest.raises(RuntimeError, match="reaped"):
        groups.signal_owned_group(process, signal.SIGKILL)
    assert not any(call[0] in {"group", "pidfd"} for call in calls)


def test_pidfd_not_child_or_start_identity_mismatch_refuses_signalling(
    model, monkeypatch
):
    process, data, _, calls = model
    groups.admit_owned_group(process)
    data[201][19] = "999"
    with pytest.raises(RuntimeError, match="changed"):
        groups.signal_owned_group(process, signal.SIGKILL)
    monkeypatch.setattr(
        groups.os, "waitid", lambda *a: (_ for _ in ()).throw(ChildProcessError())
    )
    with pytest.raises(ChildProcessError):
        groups.signal_owned_process(process, signal.SIGTERM)
    assert not any(call[0] in {"group", "pidfd"} for call in calls)


def test_live_descendant_prevents_reap_and_quiet_session_records_one_receipt(model):
    process, data, state, calls = model
    groups.admit_owned_group(process)
    exited(state, data)
    data[202] = stat_fields(202, group=201, session=201)
    assert groups.finish_owned_group(process) is False
    assert process.returncode is None
    # No signal is ever sent to the discovered numeric child.
    data[202][0] = "Z"
    assert groups.finish_owned_group(process) is True
    assert process.returncode == 0
    assert calls[-2:] == [("wait", 0), ("close", 44)]
    groups.stop_owned_groups([process])
    assert sum(call[0] == "wait" for call in calls) == 1
    assert not any(call[0] in {"group", "pidfd"} for call in calls)


def test_changed_group_within_owned_session_is_not_false_cleanup_proof(model):
    process, data, state, _ = model
    groups.admit_owned_group(process)
    exited(state, data)
    data[202] = stat_fields(202, group=202, session=201)
    assert groups.finish_owned_group(process) is False
    assert process.returncode is None


def test_batch_cleanup_escalates_while_leader_is_still_unreaped(model, monkeypatch):
    process, data, state, calls = model
    groups.admit_owned_group(process)
    data[202] = stat_fields(202, group=201, session=201)

    def killpg(pid, signum):
        assert process.returncode is None
        calls.append(("group", pid, signum))
        exited(state, data, killed=True)
        if signum == signal.SIGKILL:
            data[202][0] = "Z"

    monkeypatch.setattr(groups.os, "killpg", killpg)
    groups.stop_owned_groups([process])
    assert [call for call in calls if call[0] == "group"] == [
        ("group", 201, signal.SIGTERM),
        ("group", 201, signal.SIGKILL),
    ]
    assert 5 <= state.now < 6
    assert process.returncode == -signal.SIGKILL


def test_unverified_reaped_identity_fails_cleanup_instead_of_signalling_or_green(model):
    process, _, state, calls = model
    groups.admit_owned_group(process)
    process.returncode = 0
    with pytest.raises(RuntimeError, match="unconfirmed"):
        groups.stop_owned_groups([process])
    assert 15 <= state.now < 16
    assert not any(call[0] in {"group", "pidfd", "wait"} for call in calls)


def test_bounded_inventory_failure_cannot_claim_session_absence(model, monkeypatch):
    process, data, state, _ = model
    groups.admit_owned_group(process)
    exited(state, data)
    monkeypatch.setattr(groups, "MAX_PROC_ENTRIES", 0)
    with pytest.raises(RuntimeError, match="bound"):
        groups.finish_owned_group(process)
    assert process.returncode is None
