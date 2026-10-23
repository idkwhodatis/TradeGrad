"""Load candidate code ONLY inside the dedicated untrusted policy container.

stdout is the protocol channel, ordinary candidate output is redirected to
stderr. A hostile candidate can bypass this; the trusted relay validates it all.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

from packages.strategy_sdk.contracts import (
    parse_manifest,
    parse_observation,
    parse_reply,
)
from packages.strategy_sdk.json import canonical_json, hash_json, parse_json
from runtimes.portable_policy.protocol import (
    MAX_REPLY_BYTES,
    decision_envelope,
    read_frame,
    validate_request,
    write_frame,
)


def main(input_dir=Path("/input")):
    # Capture transport before importing any candidate. It is not a security
    # boundary: container isolation and host validation are the boundary.
    transport = sys.stdout.buffer
    sys.stdout = sys.stderr
    manifest = parse_manifest((input_dir / "manifest.json").read_bytes())
    parameters = parse_json((input_dir / "parameters.json").read_bytes())
    if (
        not isinstance(parameters, dict)
        or hash_json(parameters) != manifest.parameters_sha256
    ):
        raise ValueError("Policy parameters mismatch")
    source = input_dir / "policy.py"
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest.code_sha256:
        raise ValueError("Policy source mismatch")
    spec = importlib.util.spec_from_file_location("candidate_policy", source)
    if spec is None or spec.loader is None:
        raise ValueError("Policy source is not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    policy = module.Policy(strategy_id=manifest.strategy_id, parameters=parameters)
    method = policy.decide if manifest.strategy_kind == "portfolio" else policy.on_event
    sequence = 0
    evaluation_id = os.environ["TRADEGRAD_EVALUATION_ID"]
    while True:
        # EOF is ordinary only between frames, never within one.
        first = sys.stdin.buffer.peek(1)
        if not first:
            return
        request = validate_request(
            read_frame(sys.stdin.buffer), evaluation_id, sequence
        )
        observation = parse_observation(
            canonical_json(request["observation"]), manifest
        )
        reply = parse_reply(canonical_json(method(observation)), manifest, observation)
        write_frame(transport, decision_envelope(request, reply), MAX_REPLY_BYTES)
        sequence += 1


if __name__ == "__main__":
    main()
