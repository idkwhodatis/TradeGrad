"""Pure published-DTO selection checks; no worker, engine or database execution."""

from copy import deepcopy
from uuid import uuid4

import pytest

from script.ci_research_selection import selected_candidate


@pytest.mark.parametrize(
    "mutation", [None, "key_as_id", "key", "run", "missing", "duplicate", "no_result"]
)
def test_exact_persisted_candidate_binding(mutation):
    identifier = str(uuid4())
    run = {
        "id": str(uuid4()),
        "result": {
            "best_candidate_id": identifier,
            "best_candidate_key": "candidate_0",
        },
    }
    candidate = {"id": identifier, "run_id": run["id"], "candidate_key": "candidate_0"}
    rows = [deepcopy(candidate)]
    if mutation == "key_as_id":
        run["result"]["best_candidate_id"] = "candidate_0"
    elif mutation == "key":
        rows[0]["candidate_key"] = "candidate_1"
    elif mutation == "run":
        rows[0]["run_id"] = str(uuid4())
    elif mutation == "missing":
        rows = []
    elif mutation == "duplicate":
        rows.append(deepcopy(candidate))
    elif mutation == "no_result":
        run["result"] = None
    if mutation is None:
        assert selected_candidate(run, rows) is rows[0]
    else:
        with pytest.raises(ValueError):
            selected_candidate(run, rows)
