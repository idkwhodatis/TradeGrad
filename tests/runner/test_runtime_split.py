from importlib.metadata import PackageNotFoundError

import pytest

from script import validate_runtime_split as runtime


def _installed(values):
    def read(name):
        if name not in values:
            raise PackageNotFoundError(name)
        return values[name]

    return read


def _lock(tmp_path, relative, entries):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(f"{name}=={value}" for name, value in entries.items()))
    return target


def test_lock_parser_handles_extras_and_hash_continuations(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "# generated lock\npsycopg[binary]==3.2.1 \\\n"
        "    --hash=sha256:ignored\n    # via orchestration\nfastapi==0.120.0\n"
    )
    assert runtime.locked_versions(lock) == {
        "psycopg": "3.2.1",
        "fastapi": "0.120.0",
    }


def test_empty_lock_is_not_a_success(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("# no exact packages\n")
    with pytest.raises(ValueError, match="no exact versions"):
        runtime.locked_versions(lock)


def test_api_runtime_rejects_data_sdk_leakage(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    _lock(tmp_path, "backend/requirements.lock", {"fastapi": "0.120.0"})
    with pytest.raises(ValueError, match="must not be installed"):
        runtime.validate("api", _installed({"fastapi": "0.120.0", "akshare": "1.0"}))


def test_api_runtime_accepts_its_exact_lean_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    values = {"fastapi": "0.120.0", "psycopg": "3.2.1"}
    _lock(tmp_path, "backend/requirements.lock", values)
    assert runtime.validate("api", _installed(values)) == 2


@pytest.mark.parametrize("values", [{}, {"fastapi": "0.121.0"}])
def test_missing_or_drifted_runtime_package_fails(tmp_path, monkeypatch, values):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    _lock(tmp_path, "backend/requirements.lock", {"fastapi": "0.120.0"})
    with pytest.raises(ValueError, match="missing|differs"):
        runtime.validate("api", _installed(values))


def test_data_runtime_must_lock_every_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    _lock(tmp_path, "runtimes/data/requirements.lock", {"fastapi": "0.120.0"})
    with pytest.raises(ValueError, match="must be explicitly locked"):
        runtime.validate("data", _installed({"fastapi": "0.120.0"}))


def test_data_runtime_matches_the_independent_complete_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    values = {name: "1.0" for name in runtime.DATA_PACKAGES}
    values["psycopg"] = "3.2.1"
    _lock(tmp_path, "runtimes/data/requirements.lock", values)
    assert runtime.validate("data", _installed(values)) == len(values)


@pytest.mark.parametrize("leaked", ["numpy", "openai"])
def test_api_runtime_rejects_research_dependency_leakage(tmp_path, monkeypatch, leaked):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    _lock(tmp_path, "backend/requirements.lock", {"fastapi": "0.120.0"})
    with pytest.raises(ValueError, match="must not be installed"):
        runtime.validate("api", _installed({"fastapi": "0.120.0", leaked: "1.0"}))


def test_research_runtime_pins_algorithm_but_rejects_engine_and_providers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    values = {"fastapi": "0.120.0", "numpy": "2.3.4", "openai": "2.6.0"}
    _lock(tmp_path, "runtimes/research/requirements.lock", values)
    assert runtime.validate("research", _installed(values)) == len(values)
    for forbidden in ("akquant", "akshare", "pandas", "pyarrow"):
        with pytest.raises(ValueError, match="must not be installed"):
            runtime.validate("research", _installed({**values, forbidden: "1.0"}))


def test_research_runtime_refuses_unpinned_algorithm_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    _lock(tmp_path, "runtimes/research/requirements.lock", {"fastapi": "0.120.0"})
    with pytest.raises(ValueError, match="explicitly locked"):
        runtime.validate("research", _installed({"fastapi": "0.120.0"}))
