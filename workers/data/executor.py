"""Fenced checkpointed data import; no SDK is imported by the control-plane API."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import date, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.app.core.atomic_storage import (
    fsync_directory,
    provision_directory,
    publish_bytes,
    sync_tree,
)
from backend.app.core.config import get_settings
from backend.app.core.db import create_db_engine, session_factory
from backend.app.core.maintenance import guarded_worker
from backend.app.data_services import (
    canonical,
    claim_import,
    fence_import,
    listing_contracts,
    persist_listings,
    research_fixture_profile,
)
from backend.app.models import (
    DataBlock,
    DataImport,
    DatasetVersion,
    UniverseVersion,
    utcnow,
)
from backend.app.services import DomainError, FenceLost
from backend.app.storage import read_verified, safe_path
from packages.marketdata.contracts import (
    BarsRequest,
    ProviderResult,
    UploadMapping,
    make_listing,
)


def validate_research_fixture_policy(config, provider_name, kind, *, listings=None):
    """Reject old/relabelled profile jobs before SDK work or checkpoint reuse.

    Listing-ID requests are owner-resolved before their second admission check.
    A symbol request can be rejected before constructing a provider at all.
    """
    if provider_name != "portable_fixture":
        return
    if listings is None and config.get("listing_ids"):
        return
    profile = research_fixture_profile(
        {**config, "provider": provider_name}, listings=listings
    )
    if config.get("_execution", {}).get("fixture_profile") != profile:
        raise ValueError("Pinned synthetic fixture profile disagrees with the request")
    if profile is not None and listings is not None and kind != "resolve":
        from packages.marketdata.native_research_fixture import validate_fixture_request

        validate_fixture_request(
            BarsRequest(
                listings=listings,
                start=date.fromisoformat(config["start_date"]),
                end=date.fromisoformat(config["end_date"]),
                price_mode=config.get("price_mode", "provider_native"),
            )
        )


def is_synthetic_result(result: ProviderResult) -> bool:
    """Preserve an explicit upload declaration; never relabel it market evidence."""
    return any(p.evidence == "fixture" for p in result.provenance) or (
        result.price_semantics.native_mode == "user_declared_synthetic"
    )


def _save_checkpoint(settings, owner_id, import_id, result):
    content = canonical(result.model_dump(mode="json"))
    sha = hashlib.sha256(content).hexdigest()
    key = f"data-checkpoints/{owner_id}/{import_id}/{sha}.json"
    path = safe_path(settings.storage_root, key)
    publish_bytes(path, content, sha256=sha, durable_root=settings.storage_root)
    return key, sha


def _set_status(factory, import_id, generation, token, settings, status, **values):
    with factory.begin() as session:
        item = fence_import(session, import_id, generation, token, settings)
        item.status = status
        for key, value in values.items():
            setattr(item, key, value)
        if status in {"ready", "partial", "failed", "cancelled"}:
            item.lease_token = None
            item.lease_expires_at = None


def _resolve(factory, item_id, generation, token, settings, provider):
    with factory() as session:
        item = session.get(DataImport, item_id)
        request = item.request
        owner_id = item.owner_id
        if request.get("_derivation"):
            from packages.marketdata.contracts import Listing as ListingContract

            parent = session.get(DatasetVersion, item.parent_version_id)
            if parent is None or parent.owner_id != owner_id:
                raise ValueError("Repair parent unavailable")
            # Resolve from the immutable parent, not today's vendor mapping.
            return [
                ListingContract.model_validate(value)
                for value in parent.manifest["listings"]
            ], []
        if request.get("listing_ids"):
            return listing_contracts(session, owner_id, request["listing_ids"]), []
    resolved = []
    failures = []
    warnings = []
    provenance = []
    for symbol in request["symbols"]:
        with factory.begin() as session:
            fence_import(session, item_id, generation, token, settings)
        try:
            result = provider.resolve_instruments([symbol])
            provenance.extend([p.model_dump(mode="json") for p in result.provenance])
            if result.unresolved or len(result.data) != 1:
                failures.append(symbol)
                warnings.extend([w.model_dump(mode="json") for w in result.warnings])
                continue
            listing = result.data[0]
            if (request["market"] == "CN") != (listing.mic in {"XSHG", "XSHE"}):
                failures.append(symbol)
                continue
            with factory.begin() as session:
                current = fence_import(session, item_id, generation, token, settings)
                persist_listings(session, owner_id, [listing])
                resolved.append(listing)
                current.listing_ids = [x.listing_id for x in resolved]
                current.progress = {
                    **current.progress,
                    "resolved": len(resolved),
                    "requested_symbols": len(request["symbols"]),
                }
            warnings.extend([w.model_dump(mode="json") for w in result.warnings])
        except FenceLost:
            raise
        except Exception as exc:  # noqa: BLE001 - disclose only application-defined safe error codes
            failures.append(symbol)
            warnings.append(
                {
                    "code": exc.code
                    if isinstance(exc, DomainError)
                    else "resolution_failed",
                    "message": f"Security resolution failed: {symbol}",
                    "severity": "error",
                }
            )
    with factory.begin() as session:
        current = fence_import(session, item_id, generation, token, settings)
        current.warnings = warnings
        current.progress = {
            **current.progress,
            "unresolved": failures,
            "resolution_provenance": provenance,
        }
    return resolved, failures


def _make_blocks(factory, item_id, generation, token, settings, listings):
    with factory.begin() as session:
        item = fence_import(session, item_id, generation, token, settings)
        start = date.fromisoformat(item.request["start_date"])
        end = date.fromisoformat(item.request["end_date"])
        for listing in listings:
            cursor = start
            while cursor < end:
                # Uploaded bytes are already a bounded local snapshot, not a paged SDK.
                # Parse once and checkpoint one whole-window result to avoid raw duplication.
                block_end = (
                    end
                    if item.kind in {"upload", "probe"}
                    else min(
                        end,
                        cursor
                        + timedelta(days=item.request["_execution"]["block_days"]),
                    )
                )
                existing = session.scalar(
                    select(DataBlock).where(
                        DataBlock.import_id == item.id,
                        DataBlock.symbol == f"{listing.mic}:{listing.symbol}",
                        DataBlock.start_date == cursor,
                        DataBlock.end_date == block_end,
                    )
                )
                if existing is None:
                    session.add(
                        DataBlock(
                            import_id=item.id,
                            symbol=f"{listing.mic}:{listing.symbol}",
                            listing_id=UUID(listing.listing_id),
                            start_date=cursor,
                            end_date=block_end,
                            status="requested",
                            coverage=[],
                        )
                    )
                cursor = block_end
        session.flush()
        return [
            b.id
            for b in session.scalars(
                select(DataBlock)
                .where(DataBlock.import_id == item.id)
                .order_by(DataBlock.symbol, DataBlock.start_date)
            ).all()
        ]


def _fetch_blocks(factory, item_id, generation, token, settings, provider, listings):
    from packages.marketdata.normalize import finish_result, normalize_upload

    with factory() as session:
        item = session.get(DataImport, item_id)
        config = item.request
        owner_id = item.owner_id
        as_of = item.created_at
        kind = item.kind
    lookup = {x.listing_id: x for x in listings}
    results = []
    failed = 0
    block_ids = _make_blocks(factory, item_id, generation, token, settings, listings)
    for block_id in block_ids:
        with factory.begin() as session:
            item = fence_import(session, item_id, generation, token, settings)
            block = session.get(DataBlock, block_id)
            if block.status == "ready" and block.storage_key:
                result = ProviderResult.model_validate_json(
                    read_verified(
                        settings.storage_root,
                        block.storage_key,
                        block.content_sha256,
                        100_000_000,
                    )
                )
                results.append(result)
                continue
            block.status = "fetching"
            block.attempts += 1
            block.updated_at = utcnow()
            request = BarsRequest(
                listings=[lookup[str(block.listing_id)]],
                start=block.start_date,
                end=block.end_date,
                price_mode=config.get("price_mode", "provider_native"),
                repair_source=config.get("_derivation"),
                as_of=as_of,
            )
        try:
            if kind == "upload":
                raw = config["upload"]
                path = safe_path(settings.storage_root, raw["storage_key"])
                read_verified(
                    settings.storage_root,
                    raw["storage_key"],
                    raw["sha256"],
                    settings.max_upload_bytes,
                )
                mapping = UploadMapping.model_validate(
                    {
                        key: config[key]
                        for key in UploadMapping.model_fields
                        if key in config
                    }
                )
                result = normalize_upload(
                    path,
                    mapping,
                    request,
                    source_reference={
                        "artifact": f"source.{raw['format']}",
                        "sha256": raw["sha256"],
                        "size_bytes": raw["size_bytes"],
                    },
                )
            else:
                result = provider.fetch_bars(request)
            # Recompute independent per-listing coverage even if a provider omitted it.
            result = finish_result(request, result)
            key, sha = _save_checkpoint(settings, owner_id, item_id, result)
            complete = len(result.coverage) == 1 and all(
                c.status == "complete" for c in result.coverage
            )
            with factory.begin() as session:
                item = fence_import(session, item_id, generation, token, settings)
                block = session.get(DataBlock, block_id)
                block.status = "ready" if complete else "partial"
                block.storage_key = key
                block.content_sha256 = sha
                block.coverage = [c.model_dump(mode="json") for c in result.coverage]
                block.error = None if complete else "incomplete_coverage"
                block.updated_at = utcnow()
                item.progress = {
                    **item.progress,
                    "blocks_total": len(block_ids),
                    "blocks_completed": len(results) + 1,
                    "failed_blocks": failed + (0 if complete else 1),
                }
            results.append(result)
            if not complete:
                failed += 1
        except FenceLost:
            raise
        except Exception as exc:  # noqa: BLE001 - provider failures are reduced to bounded safe codes
            code = getattr(exc, "code", "data_block_failed")
            if not isinstance(code, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.-]{0,79}", code
            ):
                code = "data_block_failed"
            with factory.begin() as session:
                item = fence_import(session, item_id, generation, token, settings)
                block = session.get(DataBlock, block_id)
                block.status = "failed"
                block.error = code
                block.updated_at = utcnow()
                failed += 1
                item.progress = {
                    **item.progress,
                    "blocks_total": len(block_ids),
                    "blocks_completed": len(results),
                    "failed_blocks": failed,
                }
    return results, failed


def _write_universe(staging, manifest, universe, request):
    """Export exactly the pinned membership timeline, not today's basket as history."""
    import csv
    import io

    mapping = manifest["evaluator_symbol_mapping"]
    for name in list(manifest["files"]):
        if name.startswith("universe/"):
            (staging / name).unlink()
            del manifest["files"][name]
    members = universe["members"]
    boundaries = {request.start}
    historical = universe["state"] in {
        "historical_membership",
        "point_in_time_verified",
    }
    if historical:
        for member in members:
            for field in ("valid_from", "valid_to"):
                if (
                    member.get(field)
                    and request.start < date.fromisoformat(member[field]) < request.end
                ):
                    boundaries.add(date.fromisoformat(member[field]))
    for day in sorted(boundaries):
        active = [
            m
            for m in members
            if not historical
            or (
                (
                    m.get("valid_from") is None
                    or date.fromisoformat(m["valid_from"]) <= day
                )
                and (
                    m.get("valid_to") is None or day < date.fromisoformat(m["valid_to"])
                )
            )
        ]
        if not active:
            raise ValueError(
                "Existing evaluator cannot represent an empty universe interval"
            )
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["updateDate", "code", "code_name"])
        writer.writeheader()
        for member in active:
            writer.writerow(
                {
                    "updateDate": str(day),
                    "code": mapping[member["listing_id"]],
                    "code_name": mapping[member["listing_id"]],
                }
            )
        name = f"universe/members_list_{day}.csv"
        content = output.getvalue().encode()
        (staging / name).write_bytes(content)
        manifest["files"][name] = hashlib.sha256(content).hexdigest()


def _publish(factory, item_id, generation, token, settings, listings, results):
    from packages.marketdata.normalize import finish_result
    from packages.marketdata.storage import write_bundle

    with factory() as session:
        item = session.get(DataImport, item_id)
        config = item.request
        owner_id = item.owner_id
        source = item.provider
        kind = item.kind
        parent_id = item.parent_version_id
        resolution_provenance = item.progress.get("resolution_provenance", [])
        request = BarsRequest(
            listings=listings,
            start=date.fromisoformat(config["start_date"]),
            end=date.fromisoformat(config["end_date"]),
            price_mode=config.get("price_mode", "provider_native"),
            repair_source=config.get("_derivation"),
            as_of=item.created_at,
        )
        universe = (
            session.get(UniverseVersion, UUID(config["universe_version_id"]))
            if config.get("universe_version_id")
            else None
        )
        universe_value = (
            {
                "id": str(universe.id),
                "state": universe.state,
                "as_of": universe.as_of.isoformat(),
                "members": universe.members,
                "evidence": universe.evidence,
                "content_sha256": universe.content_sha256,
            }
            if universe
            else {
                "state": "user_fixed",
                "as_of": item.created_at.isoformat(),
                "members": [
                    {
                        "listing_id": x.listing_id,
                        "valid_from": None,
                        "valid_to": None,
                        "known_at": None,
                    }
                    for x in listings
                ],
                "evidence": [],
            }
        )
    combined = ProviderResult(
        data=[bar for r in results for bar in r.data],
        actions=[a for r in results for a in r.actions],
        raw=[a for r in results for a in r.raw],
        provenance=[p for r in results for p in r.provenance],
        coverage=[c for r in results for c in r.coverage],
        warnings=[w for r in results for w in r.warnings],
        price_semantics=results[0].price_semantics,
    )
    combined = finish_result(request, combined)
    if len(combined.coverage) != len(listings) or any(
        c.status != "complete" for c in combined.coverage
    ):
        _set_status(
            factory,
            item_id,
            generation,
            token,
            settings,
            "partial",
            error="Snapshot coverage is incomplete; no dataset was published",
        )
        return
    _set_status(factory, item_id, generation, token, settings, "validating")
    dataset_id = uuid4()
    storage_key = f"datasets/{owner_id}/{dataset_id}"
    target = safe_path(settings.storage_root, storage_key)
    provision_directory(target.parent, durable_parent=settings.storage_root)
    staging = target.with_name(target.name + ".staging")
    staging.mkdir(parents=True, mode=0o700)
    try:
        manifest = write_bundle(staging, request, combined)
        if not manifest["ready"]:
            _set_status(
                factory,
                item_id,
                generation,
                token,
                settings,
                "partial",
                error="Normalized snapshot failed quality validation; no dataset was published",
            )
            return
        if kind == "upload":
            upload = config["upload"]
            source_content = read_verified(
                settings.storage_root,
                upload["storage_key"],
                upload["sha256"],
                settings.max_upload_bytes,
            )
            source_name = f"source.{upload['format']}"
            publish_bytes(
                staging / source_name,
                source_content,
                sha256=upload["sha256"],
                durable_root=settings.storage_root,
            )
            manifest["artifact_files"][source_name] = upload["sha256"]
            manifest["upload_source"] = {
                "artifact": source_name,
                "sha256": upload["sha256"],
                "size_bytes": len(source_content),
            }
        _write_universe(staging, manifest, universe_value, request)
        compatibility_reasons = []
        if any(bar.volume_unit != "shares" for bar in combined.data):
            compatibility_reasons.append(
                "Evaluator requires share volume; upload explicitly converted share units in a new snapshot"
            )
        if any(bar.currency == "unknown" for bar in combined.data):
            compatibility_reasons.append("Evaluator requires a known currency")
        if config.get("unknown_flags"):
            compatibility_reasons.append(
                "Uploaded data contains unresolved semantic flags"
            )
        manifest["evaluation_compatibility"] = {
            "compatible": not compatibility_reasons,
            "reasons": compatibility_reasons,
        }
        if compatibility_reasons:
            # Preserve normalized/uncleaned data and preview, but never issue a
            # misleading legacy CSV that silently interprets lots as shares.
            for name in list(manifest["files"]):
                (staging / name).unlink()
            manifest["files"] = {}

        preview_files = {}
        for listing in listings:
            bars = [
                b.model_dump(mode="json")
                for b in combined.data
                if b.listing_id == listing.listing_id
            ]
            refs = []
            # Separate bounded pages mean API never scans an unbounded Parquet or SQL query.
            for index in range(0, len(bars), 500):
                chunk = bars[index : index + 500]
                relative = f"preview/{listing.listing_id}/{index // 500}.json"
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                content = canonical(chunk)
                path.write_bytes(content)
                refs.append(
                    {
                        "path": relative,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "start_date": chunk[0]["session_date"],
                        "end_date": str(
                            date.fromisoformat(chunk[-1]["session_date"])
                            + timedelta(days=1)
                        ),
                    }
                )
            preview_files[listing.listing_id] = refs
        manifest.update(
            schema_version=2,
            market=config["market"],
            is_synthetic=is_synthetic_result(combined),
            listings=[x.model_dump(mode="json") for x in listings],
            coverage=[c.model_dump(mode="json") for c in combined.coverage],
            provenance=[p.model_dump(mode="json") for p in combined.provenance],
            universe=universe_value,
            universe_status=universe_value["state"],
            preview_files=preview_files,
            parent_version_id=str(parent_id) if parent_id else None,
            resolution_provenance=resolution_provenance,
            import_execution_policy=config.get("_execution"),
            import_request={
                k: v
                for k, v in config.items()
                if k
                not in {
                    "upload",
                    "_execution",
                    "_derivation",
                    "_provider_connection",
                    "expected_connection_revision",
                }
            }
            if source != "upload"
            else None,
        )
        if config.get("_provider_connection") is not None:
            # Add a separate owner-configuration binding; do not rewrite provider
            # provenance identities or hash/relabel older unbound evidence.
            manifest["provider_connection"] = config["_provider_connection"]
        manifest["quality"] = {
            "warnings": [w.model_dump(mode="json") for w in combined.warnings],
            "unknown_flags": config.get("unknown_flags", []),
        }
        manifest.pop("content_hash", None)
        manifest["content_hash"] = hashlib.sha256(canonical(manifest)).hexdigest()
        # Match M1 evaluator's canonical JSON contract exactly, including Unicode escaping.
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(encoded).hexdigest()
        (staging / "manifest.json").write_bytes(encoded)
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        sync_tree(staging)
        with factory.begin() as session:
            item = fence_import(session, item_id, generation, token, settings)
            staging.rename(target)
            fsync_directory(target.parent)
            session.add(
                DatasetVersion(
                    id=dataset_id,
                    owner_id=owner_id,
                    name=config.get("name", "Market data snapshot"),
                    market=config["market"],
                    start_date=request.start,
                    end_date=request.end,
                    source=source,
                    is_synthetic=manifest["is_synthetic"],
                    content_sha256=sha,
                    manifest=manifest,
                    storage_key=storage_key,
                )
            )
            session.flush()
            item.dataset_version_id = dataset_id
            item.status = "ready"
            item.error = None
            item.lease_token = None
            item.lease_expires_at = None
            item.updated_at = utcnow()
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _record_probe(
    factory,
    item_id,
    generation,
    token,
    settings,
    results=(),
    *,
    complete=False,
    error=None,
):
    from packages.marketdata.probes import build_probe_receipt

    with factory.begin() as session:
        item = fence_import(session, item_id, generation, token, settings)
        if item.kind != "probe":
            return
        sources = [
            *item.progress.get("resolution_provenance", []),
            *[
                source.model_dump(mode="json")
                for result in results
                for source in result.provenance
            ],
        ]
        warnings = [
            *item.warnings,
            *[
                warning.model_dump(mode="json")
                for result in results
                for warning in result.warnings
            ],
        ]
        blocks = session.scalars(
            select(DataBlock).where(DataBlock.import_id == item.id)
        ).all()
        for block in blocks:
            if block.error:
                warnings.append({"code": block.error, "severity": "error"})
        if error:
            warnings.append({"code": error, "severity": "error"})
        receipt = build_probe_receipt(
            import_id=item.id,
            provider=item.provider,
            request=item.request,
            provenance=sources,
            warnings=warnings,
            complete=complete,
        )
        item.progress = {**item.progress, "probe_receipt": receipt}
        return receipt


def _verify_repair_parent(factory, item_id, settings):
    """Recheck persisted hashes before any repair fetch; never amend parent files."""
    from backend.app.data_routes import repair_parent_reference

    with factory() as session:
        item = session.get(DataImport, item_id)
        if item.request.get("price_mode") != "repair":
            return
        parent = session.get(DatasetVersion, item.parent_version_id)
        if parent is None or parent.owner_id != item.owner_id:
            raise ValueError("Repair parent unavailable")
        current = repair_parent_reference(parent, settings)
        if current.model_dump(mode="json") != item.request.get("_derivation"):
            raise ValueError("Repair parent changed")
        for name, expected in (
            ("raw.json", current.raw_sha256),
            ("provenance.json", current.provenance_sha256),
        ):
            read_verified(
                settings.storage_root,
                f"{parent.storage_key}/{name}",
                expected,
                100_000_000,
            )


@guarded_worker
def execute(import_id, generation, settings=None, factory=None):
    settings = settings or get_settings()
    factory = factory or session_factory(create_db_engine(settings))
    item_id = UUID(str(import_id))
    with factory.begin() as session:
        token = claim_import(session, item_id, generation, settings)
    if token is None:
        return
    done = threading.Event()

    def renew():
        while not done.wait(min(10, settings.data_lease_seconds / 3)):
            try:
                with factory.begin() as session:
                    fence_import(session, item_id, generation, token, settings)
            except Exception:  # noqa: BLE001 - failure is fenced and safely classified, never logged with credentials
                return

    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        from packages.marketdata.providers.common import budget_scope
        from packages.marketdata.providers.registry import get_provider
        from workers.data.rate_limit import reserve_budget

        with factory() as session:
            item = session.get(DataImport, item_id)
            kind = item.kind
            config = item.request
            provider_name = item.provider
            owner_id = item.owner_id
        from packages.marketdata.contracts import execution_policy_compatible

        execution_policy = config.get("_execution", {})
        if not execution_policy_compatible(
            execution_policy, settings.data_provider_mode, provider_name=provider_name
        ):
            _record_probe(
                factory,
                item_id,
                generation,
                token,
                settings,
                error="incompatible_execution_policy",
            )
            _set_status(
                factory,
                item_id,
                generation,
                token,
                settings,
                "failed",
                error="Pinned import execution policy is incompatible with this worker; no data was fetched or reused",
            )
            return
        validate_research_fixture_policy(config, provider_name, kind)
        from packages.marketdata.connections import validate_request_connection

        binding = validate_request_connection(config, provider_name, owner_id)
        if binding is not None:
            from backend.app.models import ProviderConnection

            with factory() as session:
                saved = session.get(
                    ProviderConnection, (binding.connection_id, binding.revision)
                )
                if (
                    saved is None
                    or saved.owner_id != owner_id
                    or saved.config != binding.model_dump(mode="json")
                ):
                    raise ValueError(
                        "Frozen provider connection revision is unavailable or mismatched"
                    )
        _verify_repair_parent(factory, item_id, settings)
        with budget_scope(lambda key: reserve_budget(factory, key, 1.0)):
            provider = (
                None
                if kind == "upload"
                else get_provider(provider_name, mode=execution_policy["provider_mode"])
            )
            if kind == "upload":
                listing = make_listing(
                    symbol=config["symbol"],
                    mic=config["mic"],
                    currency=config["currency"],
                    timezone=config["timezone"],
                    calendar=config["calendar"],
                    vendor="upload",
                    registry_key=f"upload:{owner_id}:{config['mic']}:{config['symbol']}",
                    identity_status="user_supplied",
                )
                listings = [listing]
                unresolved = []
                with factory.begin() as session:
                    item = fence_import(session, item_id, generation, token, settings)
                    persist_listings(session, owner_id, listings)
                    item.listing_ids = [listing.listing_id]
            else:
                listings, unresolved = _resolve(
                    factory, item_id, generation, token, settings, provider
                )
            if unresolved or not listings:
                _record_probe(
                    factory,
                    item_id,
                    generation,
                    token,
                    settings,
                    error="resolution_incomplete",
                )
                _set_status(
                    factory,
                    item_id,
                    generation,
                    token,
                    settings,
                    "partial" if listings else "failed",
                    error="One or more requested securities could not be resolved; no dataset was published",
                )
                return
            validate_research_fixture_policy(
                config, provider_name, kind, listings=listings
            )
            if kind == "resolve":
                _set_status(
                    factory, item_id, generation, token, settings, "normalizing"
                )
                _set_status(factory, item_id, generation, token, settings, "validating")
                _set_status(factory, item_id, generation, token, settings, "ready")
                return
            results, failed = _fetch_blocks(
                factory, item_id, generation, token, settings, provider, listings
            )
            _set_status(factory, item_id, generation, token, settings, "normalizing")
            if kind == "probe":
                complete = (
                    bool(results)
                    and not failed
                    and all(r.data for r in results)
                    and sum(len(r.coverage) for r in results) == len(listings)
                    and all(c.status == "complete" for r in results for c in r.coverage)
                )
                receipt = _record_probe(
                    factory,
                    item_id,
                    generation,
                    token,
                    settings,
                    results,
                    complete=complete,
                )
                complete = receipt["outcome"] in {"verified", "fixture_passed"}
                _set_status(
                    factory,
                    item_id,
                    generation,
                    token,
                    settings,
                    "ready" if complete else "partial",
                    error=None
                    if complete
                    else "Bounded provider test did not return complete data; inspect the scoped receipt",
                )
                return
            if failed or not results:
                _set_status(
                    factory,
                    item_id,
                    generation,
                    token,
                    settings,
                    "partial" if results else "failed",
                    error="One or more requested listing/date blocks failed validation; no dataset was published",
                )
                return
            _publish(factory, item_id, generation, token, settings, listings, results)
    except FenceLost:
        return
    except Exception:  # noqa: BLE001 - failure is fenced and safely classified, never logged with credentials
        try:
            _record_probe(
                factory,
                item_id,
                generation,
                token,
                settings,
                error="provider_test_failed",
            )
            _set_status(
                factory,
                item_id,
                generation,
                token,
                settings,
                "failed",
                error="Data import failed; inspect per-block coverage and retry after correcting the source or request",
            )
        except FenceLost:
            pass
    finally:
        done.set()
        heartbeat.join(timeout=15)
