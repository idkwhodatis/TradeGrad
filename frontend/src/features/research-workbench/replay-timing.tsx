import type {
  DecisionHeader,
  TimestampEvidence,
  TraceContext,
} from "@/client/types.gen";
import { Metadata } from "@/components/data-common";
import { marketTime } from "@/lib/replay-evidence";

// Timing meaning comes from the admitted immutable result, never from a
// browser-local timezone, run kind, or a guessed exchange-close timestamp.
export function replayTimestampPresentation(
  context?: TimestampEvidence | null,
) {
  const semantics = context?.timestamp_semantics ?? "unknown";
  const sessionLabels = semantics === "native_daily_session_labels";
  const instants = semantics === "portable_engine_instants";
  const fallback = sessionLabels
    ? "Native daily timestamps are market-local midnight session labels, not observed exchange-close or data-availability instants. Same-session OHLC does not prove availability at midnight, intraday causality or absence of look-ahead."
    : semantics === "native_recorded_unqualified"
      ? "Historical native timestamps are retained as recorded. Their session conversion and actual data-availability timing are unverified; no exchange-close instant or absence of look-ahead is inferred."
      : "Saved timestamp semantics are unavailable; no actual data-availability instant is inferred.";
  return {
    semantics,
    instants,
    cutoffLabel: sessionLabels
      ? "Information cutoff (session label)"
      : instants
        ? "Information cutoff"
        : "Information cutoff (timing unverified)",
    decisionLabel: sessionLabels
      ? "Decision session label"
      : instants
        ? "Decision time"
        : "Decision time (timing unverified)",
    caveat: context?.timestamp_caveat || (instants ? null : fallback),
    markerDescription: sessionLabels
      ? "Cutoff and decision lines use saved market-local midnight session labels on a UTC axis, not observed data-availability instants."
      : instants
        ? "Cutoff and decision lines use their exact saved instants (UTC axis)."
        : "Cutoff and decision lines preserve saved timestamp coordinates (UTC axis); their actual data-availability timing is unverified.",
  };
}

export function ReplayTimestampCaveat({
  context,
}: {
  context?: TimestampEvidence | null;
}) {
  const timing = replayTimestampPresentation(context);
  return timing.caveat ? (
    <p className="notice" data-timestamp-semantics={timing.semantics}>
      {timing.caveat}
    </p>
  ) : null;
}

export function ReplayDecisionIdentity({
  decision,
  context,
}: {
  decision: DecisionHeader;
  context: TraceContext;
}) {
  const timing = replayTimestampPresentation(context);
  return (
    <div data-testid="replay-event-identity">
      <Metadata
        values={{
          "Decision ID": decision.decision_id,
          "Source data event": decision.data_event_id,
          "Callback phase": decision.callback_phase,
          "Event sequence": decision.event_seq,
          "Decision scope": decision.scope,
          "Market timezone": decision.market_timezone,
          [timing.cutoffLabel]: marketTime(
            decision.information_cutoff,
            decision.market_timezone,
          ),
          [timing.decisionLabel]: marketTime(
            decision.decision_time,
            decision.market_timezone,
          ),
          "Session date": decision.session_date,
        }}
      />
      <ReplayTimestampCaveat context={context} />
    </div>
  );
}
