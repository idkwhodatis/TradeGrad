"""Explicit local provisioning only. Never installs demo users on application startup.

The fixtures are synthetic engine regression inputs, not market-provider coverage.
No generated strategy is imported or executed by this command.
"""

import argparse
import hashlib
import json
import os
import shutil
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select

from backend.app.core.config import get_settings
from backend.app.core.db import create_db_engine, session_factory
from backend.app.core.security import get_password_hash
from backend.app.models import DatasetVersion, StrategyVersion, User
from backend.app.services import advisory_lock
from backend.app.storage import read_verified, safe_path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "baseline" / "fixtures"


def seed_fixtures(
    session, user: User, storage_root: Path, source_root: Path = FIXTURES
):
    source_manifest = json.loads((source_root / "manifest.json").read_text())
    datasets = []
    for market in ("CN", "US"):
        files = {}
        for name, digest in source_manifest["files"].items():
            if name.startswith(market + "/"):
                files["data/" + Path(name).name] = (name, digest)
            elif name.startswith(market + "_list/"):
                files["universe/" + Path(name).name] = (name, digest)
        manifest = {
            "schema_version": 1,
            "is_synthetic": True,
            "description": source_manifest["description"],
            "provider": "none_synthetic_fixture",
            "market": market,
            "universe_status": "user_fixed",
            "price_basis": "synthetic_not_market_prices",
            "source_commit": source_manifest["source_commit"],
            "files": {name: pair[1] for name, pair in sorted(files.items())},
        }
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        content_hash = hashlib.sha256(canonical).hexdigest()
        existing = session.scalar(
            select(DatasetVersion).where(
                DatasetVersion.owner_id == user.id,
                DatasetVersion.content_sha256 == content_hash,
            )
        )
        if existing:
            datasets.append(existing)
            continue
        dataset_id = uuid4()
        key = f"datasets/{user.id}/{dataset_id}"
        target = safe_path(storage_root, key)
        staging = target.with_name(target.name + ".staging")
        staging.mkdir(parents=True, mode=0o700)
        try:
            for name, (source, digest) in files.items():
                content = read_verified(source_root, source, digest, 10_000_000)
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                destination.chmod(0o444)
            (staging / "manifest.json").write_bytes(canonical)
            (staging / "manifest.json").chmod(0o444)
            # Rename publishes the complete directory. DB reference commits afterward.
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        dataset = DatasetVersion(
            id=dataset_id,
            owner_id=user.id,
            name=f"Synthetic {market} engine fixture (NOT market data)",
            market=market,
            start_date=date.fromisoformat(source_manifest["start"]),
            end_date=date.fromisoformat(source_manifest["end"]) + timedelta(days=1),
            source="synthetic_fixture",
            is_synthetic=True,
            content_sha256=content_hash,
            manifest=manifest,
            storage_key=key,
        )
        session.add(dataset)
        datasets.append(dataset)
    strategies = []
    for task in ("cn_time", "us_time", "cn_cross", "us_cross"):
        code_bytes = (ROOT / "seed_strategies" / f"{task}.py").read_bytes()
        digest = hashlib.sha256(code_bytes).hexdigest()
        strategy = session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.owner_id == user.id,
                StrategyVersion.task == task,
                StrategyVersion.content_sha256 == digest,
            )
        )
        if not strategy:
            strategy = StrategyVersion(
                owner_id=user.id,
                name=f"Original {task} seed",
                task=task,
                code=code_bytes.decode(),
                content_sha256=digest,
                mode="native_akquant",
            )
            session.add(strategy)
        strategies.append(strategy)
    session.flush()
    return datasets, strategies


def seed_e2e_strategies(session, user: User):
    """Explicitly opt-in real sandbox probes, never substitute for original seeds."""
    original = (ROOT / "seed_strategies/cn_time.py").read_text()
    probes = {
        "E2E slow cn_time seed (20s import delay)": "import time\ntime.sleep(20)\n"
        + original,
        "E2E syntax-error cn_time seed": "# Deliberate real syntax error for failure/retry browser acceptance.\ndef deliberate_syntax_error(\n",
    }
    ids = []
    for name, code in probes.items():
        digest = hashlib.sha256(code.encode()).hexdigest()
        existing = session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.owner_id == user.id,
                StrategyVersion.content_sha256 == digest,
            )
        )
        if not existing:
            existing = StrategyVersion(
                owner_id=user.id,
                name=name,
                task="cn_time",
                code=code,
                content_sha256=digest,
                mode="native_akquant",
            )
            session.add(existing)
            session.flush()
        ids.append(str(existing.id))
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument(
        "--password-env",
        default="BOOTSTRAP_PASSWORD",
        help="Environment variable holding the initial password; never pass it on the command line",
    )
    parser.add_argument("--full-name")
    parser.add_argument("--with-synthetic-fixtures", action="store_true")
    parser.add_argument(
        "--with-e2e-fixtures",
        action="store_true",
        help="Explicit slow and broken strategy fixtures for real browser acceptance; not for research",
    )
    args = parser.parse_args()
    email = str(TypeAdapter(EmailStr).validate_python(args.email)).lower()
    settings = get_settings()
    factory = session_factory(create_db_engine(settings))
    with factory.begin() as session:
        advisory_lock(session, "bootstrap:" + email)
        user = session.scalar(select(User).where(User.email == email))
        if not user:
            password = os.environ.get(args.password_env)
            if not password:
                raise SystemExit(
                    f"Set {args.password_env} to the new user's password (12+ characters)"
                )
            user = User(
                email=email,
                hashed_password=get_password_hash(password),
                full_name=args.full_name,
            )
            session.add(user)
            session.flush()
        result = {"user_id": str(user.id), "created_or_existing": True}
        if args.with_synthetic_fixtures:
            datasets, strategies = seed_fixtures(session, user, settings.storage_root)
            result.update(
                dataset_ids=[str(d.id) for d in datasets],
                strategy_ids=[str(s.id) for s in strategies],
                is_synthetic=True,
            )
        if args.with_e2e_fixtures:
            result["e2e_strategy_ids"] = seed_e2e_strategies(session, user)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
