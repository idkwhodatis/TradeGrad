"""Offline SQL identity checks, not PostgreSQL migration execution evidence."""

import importlib
import re
from unittest.mock import Mock


def sql_statements(monkeypatch, revision, direction):
    migration = importlib.import_module(f"backend.alembic.versions.{revision}")
    execute = Mock()
    monkeypatch.setattr(migration, "op", Mock(execute=execute))
    getattr(migration, direction)()
    return [call.args[0] for call in execute.call_args_list]


def original_pin_function(monkeypatch):
    source = "\n".join(sql_statements(monkeypatch, "010_research_notes", "upgrade"))
    return re.search(
        r"CREATE FUNCTION validate_research_note_pin\(\).*?END; \$\$ LANGUAGE plpgsql;",
        source,
        re.DOTALL,
    )[0]


def normalize(statement):
    return " ".join(statement.split())


def test_note_pin_repair_changes_only_aliases_not_admission_predicates(monkeypatch):
    statements = sql_statements(monkeypatch, "011_note_pin_trigger", "upgrade")
    assert len(statements) == 1
    sql = statements[0]
    assert sql.lstrip().startswith("CREATE OR REPLACE FUNCTION")
    assert not re.search(r"\bold\b|\bcurrent\b|\bprior\b", sql, re.IGNORECASE)
    reverted = (
        sql.replace("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION")
        .replace("prior_pin", "old")
        .replace("prior_revision", "prior")
        .replace("current_revision", "current")
    )
    assert normalize(reverted) == normalize(original_pin_function(monkeypatch))


def test_note_pin_downgrade_restores_exact_010_function_without_data_writes(
    monkeypatch,
):
    statements = sql_statements(monkeypatch, "011_note_pin_trigger", "downgrade")
    assert len(statements) == 1
    reverted = statements[0].replace("CREATE OR REPLACE FUNCTION", "CREATE FUNCTION")
    assert normalize(reverted) == normalize(original_pin_function(monkeypatch))
