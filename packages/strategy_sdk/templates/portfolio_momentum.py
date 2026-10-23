"""Reference source: momentum scores and equal-weight selected targets.

Package/parse this source only on the host. Execute only inside the restricted
portable policy container. This is a new research protocol, not the paper's
native optimization, a production strategy or cross-framework certification.
"""

from packages.strategy_sdk import (
    Observation,
    PortfolioReply,
    Score,
    StateCodec,
    StateField,
    StateSchema,
    Target,
    TargetPortfolio,
    stable_signal_id,
)

STATE_SCHEMA = StateSchema(
    name="momentum",
    version=1,
    fields=(
        StateField(name="last_session", kind="string"),
        StateField(name="decisions", kind="integer"),
    ),
)


class Policy:
    def __init__(self, strategy_id: str, parameters: dict):
        if (
            set(parameters) != {"top_n"}
            or type(parameters["top_n"]) is not int
            or not 1 <= parameters["top_n"] <= 100
        ):
            raise ValueError("top_n must be an integer in [1, 100]")
        self.strategy_id = strategy_id
        self.top_n = parameters["top_n"]
        self.codec = StateCodec(STATE_SCHEMA)

    def decide(self, observation: Observation) -> PortfolioReply:
        state = self.codec.decode(observation.state)
        current = observation.session_date.isoformat()
        duplicate = state["last_session"] == current
        next_state = self.codec.encode(
            {"last_session": current, "decisions": state["decisions"] + (not duplicate)}
        )
        timing = {
            "signal_id": stable_signal_id(self.strategy_id, observation, "momentum"),
            "effective_at": observation.next_open_time,
            "expires_at": None,
        }
        if observation.warmup or duplicate:
            decision = TargetPortfolio(
                action="no_decision",
                basis="target_weight",
                omitted_positions="preserve",
                **timing,
            )
        else:
            scores = []
            for listing in observation.listings:
                bars = [
                    bar
                    for bar in observation.history
                    if bar.listing_id == listing.listing_id
                ]
                scores.append(
                    Score(
                        listing_id=listing.listing_id,
                        value=bars[-1].close / bars[0].close - 1,
                    )
                )
            selected = sorted(
                scores, key=lambda score: (-score.value, score.listing_id)
            )[: self.top_n]
            decision = TargetPortfolio(
                action="rebalance",
                basis="target_weight",
                targets=tuple(
                    Target(listing_id=score.listing_id, value=1.0 / len(selected))
                    for score in selected
                ),
                omitted_positions="liquidate",
                scores=tuple(scores),
                **timing,
            )
        return PortfolioReply(decision=decision, next_state=next_state)
