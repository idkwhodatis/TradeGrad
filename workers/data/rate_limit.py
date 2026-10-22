"""Real-upstream/account/endpoint pacing shared by all data workers via PostgreSQL."""

import hashlib
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.app.models import ProviderBudget, utcnow


def reserve_budget(factory, key, interval=1.0, account_reference="public"):
    # Account references are server-side configuration, never credential values.
    scope = hashlib.sha256(f"{key}:{account_reference}".encode()).hexdigest()
    with factory.begin() as session:
        now = utcnow()
        session.execute(
            insert(ProviderBudget)
            .values(key=scope, next_allowed_at=now)
            .on_conflict_do_nothing()
        )
        budget = session.scalar(
            select(ProviderBudget).where(ProviderBudget.key == scope).with_for_update()
        )
        slot = max(now, budget.next_allowed_at)
        wait = (slot - now).total_seconds()
        if wait > 30:
            from packages.marketdata.providers.common import ProviderError

            raise ProviderError("shared_rate_budget_exhausted", retryable=True)
        budget.next_allowed_at = slot + timedelta(seconds=interval)
    if wait:
        time.sleep(wait)
