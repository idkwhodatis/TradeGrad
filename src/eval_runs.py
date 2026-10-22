"""Score every checkpoint of a task's run on the fixed 2024-2025 test window.

Checkpoints are scored with the plain (non-CVaR, non-frequency-gated) reward over one continuous
backtest, regardless of the windowed objective they were trained under, so train -> test transfer
is measured on neutral ground.

Usage
-----
    python -m src.eval_runs --task cn_time
    python -m src.eval_runs --task cn_cross
"""
import argparse
import glob
import logging
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

from .loss import TimeStrategyReward, StrategyReward
from .strategy import Strategy
from .tasks import TASKS, TEST_START, TEST_END
from .resources import bounded_workers
from .job import load_performance

logging.basicConfig(level=logging.WARNING)


def evaluate_one(task_name, strategy_path, out_dir):
    task = TASKS[task_name]
    if task["mode"] == "time":
        loss = TimeStrategyReward(None, task["evaluate"], task["loss_kwargs"]["data_path"], TEST_START, TEST_END, market=task["market"], freq_floor=0)
    else:
        loss = StrategyReward(
            None, task["evaluate"],
            task["loss_kwargs"]["date2symbol_dir"], task["loss_kwargs"]["data_dir"],
            TEST_START, TEST_END, market=task["market"], freq_floor=0,
        )
    performance = loss.forward(Strategy(
        id=Path(strategy_path).parent.name,
        code=Path(strategy_path).read_text(),
        strategy_dir=out_dir,
    ))
    if performance.get("error"):
        raise RuntimeError(performance["error"])
    return performance


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, choices=sorted(TASKS))
    ap.add_argument("--workers", type=int, default=1)
    args = ap.parse_args()

    root = Path(TASKS[args.task]["result_root"])
    out_root = root / "checkpoint_test"
    out_root.mkdir(parents=True, exist_ok=True)

    jobs = []
    for strategy_path in glob.glob(str(root / "checkpoint" / "*" / "strategy.py")):
        out_dir = out_root / Path(strategy_path).parent.name
        try:
            load_performance(out_dir / "metrics.json")
        except (ValueError, OSError):
            pass
        else:
            continue
        jobs.append((args.task, strategy_path, out_dir))

    print(f"{len(jobs)} evaluations queued ({args.task})", flush=True)
    done = 0
    failures = 0
    with ProcessPoolExecutor(max_workers=bounded_workers(args.workers, jobs=len(jobs))) as executor:
        futures = [executor.submit(evaluate_one, *job) for job in jobs]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                failures += 1
                print(f"eval failed: {e}", flush=True)
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)
    print(f"=== eval done: {failures} failed ===", flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
