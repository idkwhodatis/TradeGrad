"""Trusted async comparison worker, with no model, strategy or Docker execution."""

import socket
import time
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.app.comparison_guard import (
    ComparisonGuardUnavailable,
    comparison_attempt_guard,
)
from backend.app.comparison_services import (
    comparison_config,
    finish_comparison_attempt,
    validate_comparison_inputs,
)
from backend.app.core.maintenance import guarded_worker
from backend.app.models import Comparison, ResearchRun
from backend.app.services import FenceLost, claim_attempt, mark_running
from packages.research_trace.comparison_contracts import (
    ComparisonError,
    ComparisonPublication,
    require_current_algorithm,
)
from workers.research.lease import AttemptLease


def _confirm_query_closure(control):
    """Never release guards on an attempted/failed close or timeout-based guess."""
    if control is None:
        return
    while True:
        try:
            control.close()
            if control.closure_confirmed:
                return
            control.cancel()
            time.sleep(0.25)
        except BaseException:  # noqa: BLE001, S110 -- preserve containment, including shutdown signals
            # A second signal or close exception is not proof of termination.
            # Process exit/SIGKILL releases the kernel flock for reconciliation.
            pass


@guarded_worker
def execute_comparison(run_id: str, generation: int, settings=None, factory=None):
    from packages.research_trace.comparison import (
        ComparisonControl,
        compare_evaluations,
    )

    worker_id = f"comparison:{socket.gethostname()}:{uuid4()}"
    with factory.begin() as session:
        attempt = claim_attempt(session, UUID(run_id), generation, worker_id, settings)
        if attempt is None:
            return {"status": "duplicate_or_stale"}
        attempt_id, token = attempt.id, attempt.lease_token
    # Claim is durable before guard acquisition. No source verification or query
    # may start before the NEW fence check performed inside this guard.
    try:
        with comparison_attempt_guard(
            settings.storage_root, UUID(run_id), attempt_id
        ) as guard:
            with factory.begin() as session:
                ready = mark_running(session, attempt_id, generation, token)
                run = session.get(ResearchRun, UUID(run_id))
                comparison = session.scalar(
                    select(Comparison).where(Comparison.run_id == run.id)
                )
                config = comparison_config(run, comparison)
            lease = AttemptLease(
                factory, settings, attempt_id, generation, token, worker_id
            )
            control = None
            status, error_code, manifest, artifacts = (
                "cancelled",
                "comparison_cancelled",
                None,
                (),
            )
            lease.start()
            try:
                if ready:
                    require_current_algorithm(config.algorithm_version)
                    control = ComparisonControl(
                        cancel_event=lease.cancelled,
                        timeout_seconds=config.request.options.max_runtime_seconds,
                    )
                    control.check()
                    with factory.begin() as session:
                        # Owner admission is inside the guard and after fresh fence.
                        run = session.get(ResearchRun, UUID(run_id))
                        comparison = session.scalar(
                            select(Comparison).where(Comparison.run_id == run.id)
                        )
                        sides = validate_comparison_inputs(
                            session, comparison, run, settings
                        )
                        control.check()
                    lease.pulse(force=True)
                    control.check()
                    manifest, artifacts = compare_evaluations(
                        sides[0].input,
                        sides[1].input,
                        guard.path / "artifacts",
                        publication=ComparisonPublication(
                            comparison_id=comparison.id,
                            run_id=run.id,
                            execution_attempt_id=attempt_id,
                        ),
                        control=control,
                        limits=config.limits,
                        durable_root=settings.storage_root,
                    )
                    control.check()
                    status, error_code = "succeeded", None
            except ComparisonError as error:
                status = (
                    "cancelled" if error.code == "comparison_cancelled" else "failed"
                )
                error_code = error.code
            except Exception:  # noqa: BLE001 -- do not disclose SQL, storage or source values
                status, error_code = "failed", "comparison_failed"
            finally:
                # Includes engine watchdog join, not a bounded join/interrupt guess.
                _confirm_query_closure(control)
                try:
                    if not lease.lost.is_set():
                        if lease.cancelled.is_set():
                            status, error_code = "cancelled", "comparison_cancelled"
                        with factory.begin() as session:
                            _item, finished_run = finish_comparison_attempt(
                                session,
                                attempt_id,
                                generation,
                                token,
                                guard=guard,
                                settings=settings,
                                status=status,
                                manifest=manifest,
                                artifacts=artifacts,
                                error_code=error_code,
                                publication_check=control.check
                                if control is not None
                                else None,
                            )
                            status = finished_run.status
                        # Guard remains held until the publication COMMIT returned.
                except FenceLost:
                    status, error_code = "interrupted", "comparison_interrupted"
                finally:
                    lease.close()
                    if lease.thread.is_alive():
                        lease.thread.join()
            return {"status": status, "query_stopped": True}
    except (ComparisonGuardUnavailable, FenceLost):
        # Unknown/busy means nonterminal. Only reconciler under row locks may
        # prove query stop, rotate token and release quota for a new generation.
        return {"status": "recovery_blocked", "query_stopped": False}
