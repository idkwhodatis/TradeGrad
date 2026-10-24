import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { ErrorState } from "@/components/common";
import { JsonDetails } from "@/components/data-common";
import { Button } from "@/components/ui/button";
import {
  clearUnreadableVerificationDraft,
  clearVerificationDraft,
  loadVerificationDraft,
  prepareVerificationDraft,
  storeVerificationDraft,
  verificationCompletionCurrent,
  verificationRequestFingerprint,
  type ControlledRequest,
  type DiagnosticRequest,
  type IndependentSource,
  type VerificationDraft,
  type VerificationKind,
} from "@/lib/verification-draft";
import { sameIndependentSource } from "@/lib/verification-evidence";

type Request = DiagnosticRequest | ControlledRequest;
/** Retain a write before dispatch. Network/401/timeouts never prove non-acceptance. */
export function useVerificationSubmission<
  T extends { request: Record<string, unknown>; id: string },
>({
  owner,
  kind,
  strategyVersionId,
  source,
  body,
  execute,
  accepted,
}: {
  owner: string;
  kind: VerificationKind;
  strategyVersionId: string;
  source: IndependentSource;
  body: Request | null;
  execute: (body: Request, key: string) => Promise<T>;
  accepted: (response: T) => void;
}) {
  const [initial] = useState(() => {
    try {
      return {
        draft: loadVerificationDraft(owner, kind),
        error: null as unknown,
      };
    } catch (error) {
      return { draft: null, error };
    }
  });
  const [draft, setDraft] = useState<VerificationDraft | null>(initial.draft);
  const [error, setError] = useState<unknown>(initial.error);
  const [unreadable, setUnreadable] = useState(Boolean(initial.error));
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const lock = useRef(false);
  const sameSource = !draft || sameIndependentSource(draft.body.source, source);
  const current = useRef({
    active: true,
    key: draft?.key ?? null,
    source,
    body: draft?.body ?? body,
  });
  current.current.source = source;
  current.current.body = draft?.body ?? body;
  current.current.key = draft?.key ?? null;
  useEffect(() => {
    current.current.active = true;
    return () => {
      current.current.active = false;
    };
  }, []);
  async function submit() {
    if (lock.current || unreadable || !sameSource || (!draft && !body)) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      const pending =
        draft ||
        (kind === "diagnostic"
          ? prepareVerificationDraft(
              "diagnostic",
              body as DiagnosticRequest,
              strategyVersionId,
            )
          : prepareVerificationDraft(
              "controlled",
              body as ControlledRequest,
              strategyVersionId,
            ));
      storeVerificationDraft(owner, pending);
      setDraft(pending);
      current.current.key = pending.key;
      current.current.body = pending.body;
      const response = await execute(pending.body, pending.key);
      if (
        verificationRequestFingerprint(response.request) !==
        verificationRequestFingerprint(pending.body)
      )
        throw new Error(
          "The accepted job does not match the exact retained request. It was not opened. Retry the same submission to resolve its outcome.",
        );
      if (verificationCompletionCurrent(pending, current.current)) {
        accepted(response);
        clearVerificationDraft(owner, kind, pending.key);
        setDraft(null);
        current.current.key = null;
        setNotice(
          `Immutable ${kind} ${response.id} accepted. Its original source remains unchanged.`,
        );
      } else if (current.current.active)
        setNotice(
          `Immutable ${kind} ${response.id} was accepted for the earlier selection. Your newer selection was not replaced. Restore the retained source and retry the same submission to open it.`,
        );
    } catch (failure) {
      if (current.current.active) setError(failure);
    } finally {
      lock.current = false;
      if (current.current.active) setBusy(false);
    }
  }
  function clearUnreadable() {
    try {
      clearUnreadableVerificationDraft(owner, kind);
      setUnreadable(false);
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  return {
    draft,
    error,
    unreadable,
    busy,
    notice,
    sameSource,
    submit,
    clearUnreadable,
  };
}
export function VerificationSubmissionState({
  state,
  kind,
}: {
  state: ReturnType<typeof useVerificationSubmission>;
  kind: VerificationKind;
}) {
  return (
    <>
      {state.draft && (
        <>
          <p className="notice">
            A {kind} submission is pending or its outcome is uncertain. Its
            complete immutable request and idempotency key are retained. Retry
            that request, never a replacement job.
          </p>
          <JsonDetails
            title={`Exact retained ${kind} submission`}
            value={state.draft.body}
          />
        </>
      )}
      {state.draft && !state.sameSource && (
        <Button asChild variant="outline">
          <Link
            to="/strategies/$strategyId/research"
            params={{ strategyId: state.draft.strategy_version_id }}
            search={state.draft.selection}
          >
            Restore pending {kind} source
          </Link>
        </Button>
      )}
      {state.unreadable && (
        <Button variant="outline" onClick={state.clearUnreadable}>
          Clear unreadable local {kind} request
        </Button>
      )}
      {state.error != null && <ErrorState error={state.error} />}
      {state.notice && (
        <p className="notice" role="status">
          {state.notice}
        </p>
      )}
    </>
  );
}
