"""Execute all four unchanged seed strategies through real AKQuant, without any model calls."""
import argparse
import hashlib
import importlib.metadata
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.loss import run_window_backtest, run_time_window_backtest
from src.tasks import TASKS


def run(output: Path) -> dict:
    fixture = ROOT / "tests/baseline/fixtures"
    manifest = json.loads((fixture / "manifest.json").read_text())
    for filename, expected in manifest["files"].items():
        if hashlib.sha256((fixture / filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen fixture was modified: {filename}")
    results = {}
    for name, task in TASKS.items():
        market = task["market"]
        target = output / name
        if target.exists():
            raise ValueError(f"Refusing to overwrite baseline attempt: {target}")
        args = (task["initial"], target, task["evaluate"])
        if task["mode"] == "time":
            result = run_time_window_backtest(*args, str(fixture / market / "INDEX.csv"), "target_symbol", manifest["start"], manifest["end"], market)
        else:
            result = run_window_backtest(*args, str(fixture / f"{market}_list"), str(fixture / market), manifest["start"], manifest["end"], market)
        results[name] = result
        print(f"{name}: {'FAILED' if 'error' in result else 'passed'}", flush=True)
    report = {"schema_version": 1, "profile": "corrected_explicit_config", "fixture_manifest_sha256": hashlib.sha256((fixture / "manifest.json").read_bytes()).hexdigest(), "versions": {name: importlib.metadata.version(name) for name in ("akquant", "pandas", "numpy")}, "results": results, "passed": all("error" not in result for result in results.values()), "llm_calls": 0}
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False)+"\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "result/baseline" / uuid.uuid4().hex)
    args = parser.parse_args()
    report = run(args.output)
    print(f"Evidence: {args.output / 'report.json'}")
    raise SystemExit(0 if report["passed"] else 1)
