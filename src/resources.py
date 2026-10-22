"""Conservative per-process resource budgets; the isolated runner owns global quotas."""
import os


def bounded_workers(requested: int | None = None, *, limit_var: str = "TRADEGRAD_WORKERS", jobs: int | None = None) -> int:
    limit = int(os.environ.get(limit_var, "1"))
    if limit < 1 or (requested is not None and requested < 1):
        raise ValueError("Worker counts must be positive")
    count = min(requested or limit, limit)
    return min(count, max(1, jobs)) if jobs is not None else count
