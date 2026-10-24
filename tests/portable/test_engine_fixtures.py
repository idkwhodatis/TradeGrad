"""Native fixture dependencies must not include the pytest test runner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_shared_native_fixtures_work_without_pytest():
    root = Path(__file__).resolve().parents[2]
    # Use the same frozen native interpreter as the actual-engine tests when
    # selected. Blocking imports also catches the regression on developer
    # machines whose interpreter happens to have pytest installed.
    interpreter = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON", sys.executable)
    script = r"""
import sys
from itertools import product
from types import SimpleNamespace

class NoPytest:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pytest', '_pytest'}:
            raise ModuleNotFoundError('Native fixtures must not require pytest', name=fullname)

assert not any(name.split('.')[0] in {'pytest', '_pytest'} for name in sys.modules)
sys.meta_path.insert(0, NoPytest())
from packages.strategy_sdk import StateCodec, Target
from tests.portable import engine_fixtures as f

for market, kind, count, prewarm in product(('US', 'CN'), ('portfolio', 'event'), (1, 2), (False, True)):
    request, data = f.fixture(market, kind, count, prewarm)
    loaded = f.changed_input(request, data)
    assert loaded == f.inputs(market, kind, count, prewarm)
    session = loaded.sessions[0]
    obs = SimpleNamespace(
        warmup=prewarm, session_date=session.session_date,
        next_open_time=session.next_open_time,
        state=StateCodec(f.SCHEMA).encode({'counter': 0}),
    )
    reply = f.portfolio_reply(obs, 'rebalance', (Target(listing_id=f.L1, value=10),))
    assert reply['decision']['action'] == ('no_decision' if prewarm else 'rebalance')
    assert reply['next_state']['payload_json'] == '{"counter":1}'
    event = f.event_reply(obs, (f.order(obs),))
    assert event['decision']['intents'][0]['listing_id'] == f.L1
    assert event['next_state'] == reply['next_state']

assert 'tests.portable.test_engine' not in sys.modules
assert not any(name.split('.')[0] in {'pytest', '_pytest'} for name in sys.modules)
"""
    result = subprocess.run(
        [interpreter, "-B", "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
