"""Pure fake-process and journal checks: no fork, sockets, signals or strategies."""

import json
import secrets
import signal
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.core.config import Settings
from packages.tradegrad_adapter import model_calls, model_sdk_process
from packages.tradegrad_adapter.config import BudgetConfig, ModelConfig
from packages.tradegrad_adapter.model_calls import (
    EnvironmentOpenAIModel,
    ModelProviderFailure,
    ModelRequest,
    ModelResponse,
    OptimizerStop,
    RecordedModel,
)
from packages.tradegrad_adapter.model_profiles import OperatorModelProfile
from workers.research.model_provider import create_model


def profile(**updates):
    return OperatorModelProfile(
        id="bounded",
        label="Synthetic unit profile",
        model="model-a",
        base_url="https://models.example.com/v1",
        secret_env="UNIT_MODEL_KEY",
        owner_ids=[uuid4()],
        request_timeout_seconds=2,
        **updates,
    )


def provider(monkeypatch):
    selected = profile()
    monkeypatch.setenv(selected.secret_env, secrets.token_urlsafe(32))
    return EnvironmentOpenAIModel(profile=selected, live_enabled=True)


def request(**updates):
    return ModelRequest(
        **{
            "call_id": "unit:0000",
            "logical_index": 0,
            "user_prompt": "synthetic",
            "system_prompt": None,
            "max_output_tokens": 20,
            "timeout_seconds": 30,
            **updates,
        }
    )


def success(text="synthetic reply", **updates):
    return json.dumps(
        {
            "ok": True,
            "response": {
                "text": text,
                "input_tokens": 3,
                "output_tokens": 2,
                "provider_request_id": "synthetic-id",
                "usage_source": "provider",
                **updates,
            },
        }
    ).encode()


class FakeProcess:
    pid = 10101

    def __init__(self, output=None, timeout=False, refuse_stop=False, returncode=0):
        self.output = success() if output is None else output
        self.timeout = timeout
        self.refuse_stop = refuse_stop
        self.returncode = None
        self.exit_code = returncode
        self.calls = []

    def poll(self):
        return self.returncode

    def communicate(self, data=None, timeout=None):
        self.calls.append((data, timeout))
        if self.timeout or self.refuse_stop:
            self.timeout = False
            raise subprocess.TimeoutExpired("trusted-model", timeout)
        self.returncode = self.exit_code
        return self.output, None


def install_process(monkeypatch, process):
    captured, killed = [], []

    def spawn(args, **kwargs):
        captured.append((args, kwargs))
        return process

    monkeypatch.setattr(model_calls.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        model_calls.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )
    return captured, killed


def journal(tmp_path, model, **kwargs):
    item = RecordedModel(
        tmp_path,
        input_digest="a" * 64,
        provider=model,
        budgets={"max_model_calls": 2, "max_tokens": 10000, "max_runtime_seconds": 30},
        parameters={"temperature": 0, "max_output_tokens": 20},
        **kwargs,
    )
    item.set_context("synthetic")
    return item


def test_worker_only_factory_missing_secret_and_default_fixture_no_process(monkeypatch):
    selected = profile()
    config = Settings(
        database_url="postgresql://localhost/not_used",
        secret_key=secrets.token_urlsafe(32),
        model_profiles_enabled=True,
        model_profiles=[selected],
    )
    owner = selected.owner_ids[0]
    monkeypatch.delenv(selected.secret_env, raising=False)
    called = []
    monkeypatch.setattr(
        model_calls.subprocess, "Popen", lambda *_a, **_k: called.append(True)
    )
    fixture = create_model(
        SimpleNamespace(model=ModelConfig(), budgets=BudgetConfig()),
        config,
        owner,
        "# immutable seed",
    )
    assert fixture.complete(request()).text == "```python\n# immutable seed\n```"
    assert called == []
    with pytest.raises(ModelProviderFailure, match="model_credential_unavailable"):
        create_model(
            SimpleNamespace(model=selected.public_config()), config, owner, "# seed"
        )
    assert called == []


def test_secret_only_private_pipe_and_deadline_not_inherited_environment(monkeypatch):
    model = provider(monkeypatch)
    monkeypatch.setenv("UNRELATED_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.example.com")
    fake = FakeProcess()
    captured, killed = install_process(monkeypatch, fake)
    result = model.complete(request(timeout_seconds=1.5))
    assert result.text == "synthetic reply" and model.contained
    assert not killed and model.close()
    args, options = captured[0]
    assert args[1] == "-I" and args[-1].endswith("model_sdk_process.py")
    assert options["start_new_session"] is True
    assert options["stderr"] == subprocess.DEVNULL
    assert set(options["env"]) == {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE"}
    if model._secret in json.dumps([args, options]):
        pytest.fail("Credential material escaped private pipe", pytrace=False)
    wire, timeout = fake.calls[0]
    assert timeout == 1.5
    payload = json.loads(wire)
    if payload["secret"] != model._secret:
        pytest.fail("Private selected credential differs", pytrace=False)
    assert payload["timeout_seconds"] == 1.5
    assert payload["parent_pid"] == model_calls.os.getpid()
    if "UNRELATED_SECRET" in payload:
        pytest.fail("Unrelated material escaped environment isolation", pytrace=False)


@pytest.mark.parametrize("refuse_stop", [False, True])
def test_timeout_stays_uncertain_and_uncontained_cannot_finalize(
    monkeypatch, tmp_path, refuse_stop
):
    model = provider(monkeypatch)
    fake = FakeProcess(timeout=True, refuse_stop=refuse_stop)
    captured, killed = install_process(monkeypatch, fake)
    item = journal(tmp_path, model)
    with pytest.raises(OptimizerStop, match="model_call_outcome_unknown"):
        item.query("test prompt")
    assert len(captured) == 1 and killed == [(fake.pid, signal.SIGKILL)]
    assert item.records[0]["status"] == "started"
    assert item.records[0]["charged_tokens"] > 0
    assert model.contained is (not refuse_stop)
    replay = journal(tmp_path, model)
    with pytest.raises(OptimizerStop, match="model_call_outcome_unknown"):
        replay.query("test prompt")
    assert len(captured) == 1
    assert model.close() is (not refuse_stop)


@pytest.mark.parametrize(
    "output,returncode", [(b"not protocol", 0), (b"", -9), (b"{}", 0)]
)
def test_child_crash_or_unparseable_result_never_retries(
    monkeypatch, tmp_path, output, returncode
):
    model = provider(monkeypatch)
    fake = FakeProcess(output=output, returncode=returncode)
    captured, _ = install_process(monkeypatch, fake)
    item = journal(tmp_path, model)
    with pytest.raises(OptimizerStop, match="model_call_outcome_unknown"):
        item.query("test prompt")
    assert len(captured) == 1 and model.contained
    assert item.records[0]["status"] == "started"


def test_response_request_id_and_prompt_secret_route_reference_redaction(
    monkeypatch, tmp_path
):
    model = provider(monkeypatch)
    marker = " ".join(model.redactions)
    fake = FakeProcess(output=success(marker, provider_request_id=model._secret))
    install_process(monkeypatch, fake)
    item = journal(tmp_path, model)
    item.query("test prompt " + marker)
    rendered = json.dumps(item.records)
    persisted = next(tmp_path.glob("*.json")).read_text()
    sent = json.loads(fake.calls[0][0])["messages"][0]["content"]
    for secret in model.redactions:
        if secret in rendered or secret in persisted or secret in sent:
            pytest.fail("Private operator material escaped redaction", pytrace=False)
    assert item.records[0]["status"] == "completed"


def test_cancel_during_request_waits_then_persists_completed_response(
    monkeypatch, tmp_path
):
    model = provider(monkeypatch)
    fake = FakeProcess()
    captured, _ = install_process(monkeypatch, fake)
    state = [None]
    communicate = fake.communicate

    def finish_then_cancel(*args, **kwargs):
        state[0] = "cancel"
        return communicate(*args, **kwargs)

    fake.communicate = finish_then_cancel
    item = journal(tmp_path, model, control=lambda: state[0])
    with pytest.raises(OptimizerStop, match="cancelled"):
        item.query("test prompt")
    assert model.contained and item.records[0]["status"] == "completed"
    replay = journal(tmp_path, model)
    assert replay.query("test prompt") == "synthetic reply"
    assert len(captured) == 1


def test_pre_request_cancel_and_exhausted_budget_start_no_process(
    monkeypatch, tmp_path
):
    model = provider(monkeypatch)
    captured, _ = install_process(monkeypatch, FakeProcess())
    item = journal(tmp_path, model, control=lambda: "cancel")
    with pytest.raises(OptimizerStop, match="cancelled"):
        item.query("test prompt")
    assert item.records == [] and captured == []
    item = journal(tmp_path, model)
    item.budgets["max_tokens"] = 0
    with pytest.raises(OptimizerStop, match="max_tokens"):
        item.query("test prompt")
    assert item.records == [] and captured == []


def test_parent_death_race_and_alarm_installed_without_real_signals(monkeypatch):
    calls = []
    monkeypatch.setattr(model_sdk_process.sys, "platform", "linux")
    monkeypatch.setattr(
        model_sdk_process.ctypes,
        "CDLL",
        lambda *_a, **_kw: SimpleNamespace(
            prctl=lambda *args: calls.append(("prctl", args)) or 0
        ),
    )
    monkeypatch.setattr(model_sdk_process.os, "getppid", lambda: 42)
    monkeypatch.setattr(
        model_sdk_process.signal, "signal", lambda *args: calls.append(("signal", args))
    )
    monkeypatch.setattr(
        model_sdk_process.signal,
        "setitimer",
        lambda *args: calls.append(("alarm", args)),
    )
    model_sdk_process.contain_process({"parent_pid": 42, "timeout_seconds": 2})
    assert [item[0] for item in calls] == ["prctl", "signal", "alarm"]
    assert calls[0][1] == (1, signal.SIGKILL, 0, 0, 0)
    calls.clear()
    with pytest.raises(RuntimeError, match="containment_unavailable"):
        model_sdk_process.contain_process({"parent_pid": 41, "timeout_seconds": 2})
    assert [item[0] for item in calls] == ["prctl"]
    calls.clear()
    monkeypatch.setattr(model_sdk_process.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="containment_unavailable"):
        model_sdk_process.contain_process({"parent_pid": 42, "timeout_seconds": 2})
    assert calls == []


def test_invalid_usage_remains_reserved_and_never_replayed_as_completed(
    monkeypatch, tmp_path
):
    model = provider(monkeypatch)
    monkeypatch.setattr(
        model, "complete", lambda _request: ModelResponse("text", -1, 2)
    )
    item = journal(tmp_path, model)
    with pytest.raises(OptimizerStop, match="invalid_model_usage"):
        item.query("test prompt")
    assert item.records[0]["status"] == "started"
    assert item.records[0]["charged_tokens"] > 0


def test_durable_admission_precedes_process_and_stop_follows_exit(monkeypatch):
    model = provider(monkeypatch)
    fake = FakeProcess()
    install_process(monkeypatch, fake)
    observed = []

    def receipt(phase, call_id):
        observed.append((phase, call_id, fake.poll()))

    model.boundary = receipt
    model.complete(request())
    assert observed == [("admitted", "unit:0000", None), ("stopped", "unit:0000", 0)]


@pytest.mark.parametrize("phase", ["admitted", "stopped"])
def test_uncertain_receipt_never_leaks_persistence_errors_or_releases_recovery(
    monkeypatch, tmp_path, phase
):
    model = provider(monkeypatch)
    fake = FakeProcess()
    captured, _ = install_process(monkeypatch, fake)
    diagnostic_secret = secrets.token_urlsafe(32)

    def receipt(current, _call_id):
        if current == phase or phase == "admitted":
            raise RuntimeError(diagnostic_secret)

    model.boundary = receipt
    item = journal(tmp_path, model)
    with pytest.raises(OptimizerStop, match="model_call_outcome_unknown"):
        item.query("synthetic")
    assert len(captured) == (1 if phase == "stopped" else 0)
    assert item.records[0]["status"] == "started"
    assert model.close() is False
    if diagnostic_secret in json.dumps(item.records):
        pytest.fail("Persistence diagnostics leaked", pytrace=False)


def test_uncertain_launch_interruption_never_publishes_false_stop(monkeypatch):
    model = provider(monkeypatch)
    observed = []
    model.boundary = lambda phase, call_id: observed.append((phase, call_id))

    def interrupted_launch(*_args, **_kwargs):
        raise KeyboardInterrupt("synthetic launch uncertainty")

    monkeypatch.setattr(model_calls.subprocess, "Popen", interrupted_launch)
    with pytest.raises(KeyboardInterrupt):
        model.complete(request())
    assert model.contained is False
    assert model.close() is False
    assert observed == [("admitted", "unit:0000")]


def test_known_failed_exec_can_confirm_no_child(monkeypatch):
    model = provider(monkeypatch)
    observed = []
    model.boundary = lambda phase, call_id: observed.append((phase, call_id))

    def failed_exec(*_args, **_kwargs):
        raise OSError("synthetic failed exec")

    monkeypatch.setattr(model_calls.subprocess, "Popen", failed_exec)
    with pytest.raises(ModelProviderFailure, match="model_process_unavailable"):
        model.complete(request())
    assert model.contained is True and model.close()
    assert observed == [("admitted", "unit:0000"), ("stopped", "unit:0000")]
