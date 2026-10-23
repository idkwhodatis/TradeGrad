"""Reference source: completed-bar trend intents, never a host-executed module."""

from packages.strategy_sdk import (
    EventDecision,
    EventReply,
    Observation,
    OrderIntent,
    StateCodec,
    StateField,
    StateSchema,
    stable_signal_id,
)

STATE_SCHEMA = StateSchema(
    name="trend", version=1, fields=(StateField(name="last_session", kind="string"),)
)


class Policy:
    def __init__(self, strategy_id: str, parameters: dict):
        if (
            set(parameters) != {"quantity"}
            or type(parameters["quantity"]) is not int
            or not 1 <= parameters["quantity"] <= 1_000_000
        ):
            raise ValueError("quantity must be a positive bounded share count")
        self.strategy_id = strategy_id
        self.quantity = parameters["quantity"]
        self.codec = StateCodec(STATE_SCHEMA)

    def on_event(self, observation: Observation) -> EventReply:
        state = self.codec.decode(observation.state)
        current = observation.session_date.isoformat()
        next_state = self.codec.encode({"last_session": current})
        if observation.warmup or state["last_session"] == current:
            return EventReply(decision=EventDecision(), next_state=next_state)
        positions = {
            position.listing_id: position for position in observation.account.positions
        }
        pending = {order.listing_id for order in observation.open_orders}
        intents = []
        for listing in observation.listings:
            if listing.listing_id in pending:
                continue
            bars = [
                bar
                for bar in observation.history
                if bar.listing_id == listing.listing_id
            ]
            if len(bars) < 2:
                continue
            position = positions.get(listing.listing_id)
            rising = bars[-1].close > bars[-2].close
            if rising and (position is None or position.quantity == 0):
                side, quantity, effect, reduce = (
                    "buy",
                    float(self.quantity),
                    "open",
                    False,
                )
            elif (
                not rising and position is not None and position.available_quantity > 0
            ):
                # This labelled capacity applies only to the next session.
                # Native T+0 availability is never presented as CN T+1 sellability.
                side, quantity, effect, reduce = (
                    "sell",
                    position.available_quantity,
                    "close",
                    True,
                )
            else:
                continue
            intents.append(
                OrderIntent(
                    signal_id=stable_signal_id(
                        self.strategy_id, observation, f"{listing.listing_id}:{side}"
                    ),
                    listing_id=listing.listing_id,
                    side=side,
                    quantity=quantity,
                    order_type="market",
                    time_in_force="GTC",
                    reduce_only=reduce,
                    position_effect=effect,
                    effective_at=observation.next_open_time,
                    expires_at=None,
                )
            )
        return EventReply(
            decision=EventDecision(intents=tuple(intents)), next_state=next_state
        )
