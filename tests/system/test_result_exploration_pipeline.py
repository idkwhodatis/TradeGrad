"""Opt-in real HTTP/PG/queue/data-worker/Docker result-exploration gate.

Never run locally where PG, sockets, Docker or strategy execution is unavailable.
--collect-only imports no strategy or engine and starts no services. Synthetic
prices and frozen models do not constitute live-provider or trading evidence.
"""

import hashlib
import json
from uuid import uuid4

import test_research_optimization as research

research_harness = research.research_harness

from packages.tradegrad_adapter.config import MarketConfig

# Stored as source through the real API. Only the production Docker evaluator
# may execute this fixture; no Python exec/import/subprocess of this code here.
STRATEGY = """from akquant import Strategy
class RecordedIndicatorFixture(Strategy):
    def __init__(self):
        super().__init__()
        self.n = 0
    def on_start(self):
        self.subscribe("target_symbol")
    def on_bar(self, bar):
        self.n += 1
        self.record_indicator("observed_close", float(bar.close), symbol=bar.symbol,
                              display_name="Declared observed close", unit="currency_per_share", pane=0)
        self.record_indicator("observed_counter", float(self.n), symbol=bar.symbol,
                              display_name="Declared callback count", unit="count", pane=1)
        if self.n % 8 == 1 and self.get_position(bar.symbol) == 0:
            self.buy(symbol=bar.symbol, quantity=100)
        elif self.n % 8 == 5 and self.get_position(bar.symbol) > 0:
            self.sell(symbol=bar.symbol, quantity=100)
"""


def test_real_recorded_indicators_reconstruct_from_artifact_after_worker_completion(
    research_harness,
):
    h = research_harness
    dataset, universe = h.upload_fixture()
    client, other = h.clients
    strategy = h.request(
        client,
        "POST",
        "/strategy-versions",
        status=201,
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "name": "Synthetic recorded-indicator system fixture",
            "task": "cn_time",
            "code": STRATEGY,
        },
    ).json()
    capabilities = h.request(client, "GET", "/research-capabilities").json()
    config = {
        "execution_mode": "optimization",
        "task": "cn_time",
        "dataset": {"id": dataset["id"], "sha256": dataset["content_sha256"]},
        "seed_strategy": {"id": strategy["id"], "sha256": strategy["content_sha256"]},
        "universe": {"id": universe["id"], "sha256": universe["content_sha256"]},
        "windows": {
            "train": {"start": "2024-02-01", "end": "2024-03-15"},
            "validation": {"start": "2024-03-15", "end": "2024-04-20"},
        },
        "warmup": {"sessions": 20},
        "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
        "optimizer": {"max_iterations": 0},
        "environment_digest": capabilities["environment_digest"],
    }
    run = h.request(
        client,
        "POST",
        "/research-runs",
        status=202,
        headers={"Idempotency-Key": uuid4().hex},
        json=config,
    ).json()
    h.runs.append({"id": run["id"], "client": client, "config": config})
    completed = h.until(
        lambda: h.finished(run["id"]),
        "Real recorded-indicator evaluation did not complete",
        timeout=100,
    )
    assert completed["attempts"][0]["sandbox_stopped"]
    evaluations = h.request(
        client, "GET", f"/research-runs/{run['id']}/evaluations"
    ).json()["data"]
    assert len(evaluations) == 2
    assert {item["split"] for item in evaluations} == {"train", "validation"}
    assert all(
        item["status"] == "succeeded" and item["sandbox_stopped"]
        for item in evaluations
    )
    [evaluation] = [item for item in evaluations if item["split"] == "validation"]
    assert evaluation["status"] == "succeeded" and evaluation["sandbox_stopped"]
    base = f"/research-evaluations/{evaluation['id']}"
    raw = h.request(client, "GET", base + "/results/download").content
    bundle = json.loads(raw)
    assert bundle["indicators"]["status"] == "available"
    assert {item["indicator_key"] for item in bundle["indicators"]["definitions"]} == {
        "observed_close",
        "observed_counter",
    }
    listing = str(
        h.request(client, "GET", f"/datasets/{dataset['id']}").json()["manifest"][
            "listings"
        ][0]["listing_id"]
    )
    replay = h.request(
        client,
        "GET",
        base + "/price-replay",
        params={"listing_id": listing, "limit": 500},
    ).json()
    assert replay["artifact_sha256"] == hashlib.sha256(raw).hexdigest()
    assert replay["indicators_status"] == "available" and len(replay["indicators"]) == 2
    assert replay["markers"] and all(item["on_price_bar"] for item in replay["markers"])
    close_series = next(
        item
        for item in replay["indicators"]
        if item["metadata"]["indicator_key"] == "observed_close"
    )
    prices = {bar["session_date"]: bar["close"] for bar in replay["bars"]}
    assert close_series["points"]
    assert all(
        point["value"] == prices[point["session_date"]]
        for point in close_series["points"]
    )
    analysis = h.request(client, "GET", base + "/return-analysis").json()
    assert analysis["valid_returns"] > 0
    assert sum(item["count"] for item in analysis["bins"]) == analysis["valid_returns"]
    assert (
        h.request(client, "GET", base + "/results").json()["metrics"]
        == bundle["metrics"]
    )
    assert h.request(client, "GET", base + "/results/download").content == raw
    h.request(other, "GET", base + "/return-analysis", status=404)
    h.request(
        other, "GET", base + "/price-replay", params={"listing_id": listing}, status=404
    )
