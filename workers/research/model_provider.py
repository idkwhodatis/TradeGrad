"""Trusted worker-only credential resolution. Never imported by evaluators."""

from packages.tradegrad_adapter.model_profiles import resolve_profile


def create_model(config, settings, owner_id, seed_code, *, boundary=None):
    from packages.tradegrad_adapter.model_calls import (
        EnvironmentOpenAIModel,
        FrozenModel,
    )

    profile = resolve_profile(config.model, settings, owner_id)
    if profile is None:
        return FrozenModel(
            [f"```python\n{seed_code}\n```"] * config.budgets.max_model_calls
        )
    return EnvironmentOpenAIModel(profile=profile, live_enabled=True, boundary=boundary)
