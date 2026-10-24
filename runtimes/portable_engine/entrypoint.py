"""Data-only engine entrypoint. Stdout is exclusively the framed trusted channel."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from runtimes.portable_policy.protocol import (
    MAX_OBSERVATION_BYTES,
    MAX_REPLY_BYTES,
    MAX_RESULT_BYTES,
    observation_envelope,
    read_frame,
    result_envelope,
    validate_response,
    write_frame,
)

from .runtime import MAX_DATA_BYTES, execute, load_inputs


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("missing, symlinked or oversized trusted data artifact")
    return path.read_bytes()


def main(input_dir=Path("/input")):
    # Preserve a dedicated binary protocol FD, divert Python AND native stdout
    # before AKQuant import. No engine log may impersonate a protocol frame.
    wire = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    evaluation_id = os.environ["TRADEGRAD_EVALUATION_ID"]
    sequence = 0
    inputs = load_inputs(
        _read(Path(input_dir) / "request.json", MAX_OBSERVATION_BYTES),
        _read(Path(input_dir) / "market.json", MAX_DATA_BYTES),
    )

    def exchange(observation):
        nonlocal sequence
        request = observation_envelope(evaluation_id, sequence, observation)
        write_frame(wire, request, MAX_OBSERVATION_BYTES)
        response = read_frame(sys.stdin.buffer, MAX_REPLY_BYTES)
        validate_response(response, request)
        sequence += 1
        return response["reply"]

    from packages.research_trace.contracts import CaptureConfig
    from packages.research_trace.recorder import Recorder
    from packages.research_trace.transport import MAX_FRAME_BYTES, CaptureWriter

    config = inputs.capture_config or CaptureConfig()
    capture_writer = CaptureWriter(
        config, lambda value: write_frame(wire, value, MAX_FRAME_BYTES)
    )
    recorder = Recorder(
        code_sha256=inputs.manifest.code_sha256,
        config=config,
        on_record=capture_writer.append,
        source="engine_observed",
    )
    result = execute(inputs, exchange, research_recorder=recorder)
    capture_writer.finish(recorder.summary())
    write_frame(
        wire, result_envelope(evaluation_id, sequence, result), MAX_RESULT_BYTES
    )
    wire.close()


if __name__ == "__main__":
    main()
