"""Match persisted application identities, never confuse optimizer keys with UUIDs."""


def selected_candidate(run, candidates):
    result = run.get("result") or {}
    identifier, key = result.get("best_candidate_id"), result.get("best_candidate_key")
    if not identifier or not key:
        raise ValueError("Completed research has no persisted candidate identity")
    matches = [row for row in candidates if row["id"] == identifier]
    if (
        len(matches) != 1
        or matches[0]["run_id"] != run["id"]
        or matches[0]["candidate_key"] != key
    ):
        raise ValueError("Selected candidate UUID/key/run binding differs")
    return matches[0]
