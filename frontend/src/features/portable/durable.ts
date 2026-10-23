import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import type { PortablePreflightPublic } from "@/client/types.gen";
import {
  portableFingerprint,
  portableFormSchema,
  type PortableForm,
} from "./protocol";
export type PortableSubmission = { key: string; body: PortableForm };
export type PortableDraft = {
  version: 1;
  body: PortableForm;
  pending: PortableSubmission | null;
};
export type PortablePreflight = {
  fingerprint: string;
  result: PortablePreflightPublic;
};
const submissionSchema = z.strictObject({
  key: z.uuid(),
  body: portableFormSchema,
});
const draftSchema = z.strictObject({
  version: z.literal(1),
  body: portableFormSchema,
  pending: submissionSchema.nullable(),
});
const storageKey = (owner: string) =>
  `tradegrad.portable-draft.v1.${encodeURIComponent(owner)}`;
export function loadPortableDraft(owner: string): PortableDraft | null {
  try {
    const result = draftSchema.safeParse(
      JSON.parse(sessionStorage.getItem(storageKey(owner)) || "null"),
    );
    return result.success ? result.data : null;
  } catch {
    return null;
  }
}
export function savePortableDraft(
  owner: string,
  draft: PortableDraft,
): boolean {
  try {
    const parsed = draftSchema.safeParse(draft);
    if (!parsed.success) return false;
    sessionStorage.setItem(storageKey(owner), JSON.stringify(parsed.data));
    return true;
  } catch {
    return false;
  }
}
export function portableSubmissionFor(
  body: PortableForm,
  previous: PortableSubmission | null,
): PortableSubmission {
  const parsed = portableFormSchema.parse(body);
  return previous &&
    portableFingerprint(previous.body) === portableFingerprint(parsed)
    ? previous
    : { key: crypto.randomUUID(), body: structuredClone(parsed) };
}
export function portableCompletionIsCurrent(
  completed: PortableSubmission,
  current: {
    active: boolean;
    owner: string;
    requestOwner: string;
    body: PortableForm;
    pending: PortableSubmission | null;
  },
) {
  return (
    current.active &&
    current.owner === current.requestOwner &&
    current.pending?.key === completed.key &&
    portableFingerprint(current.body) === portableFingerprint(completed.body) &&
    portableFingerprint(current.pending.body) ===
      portableFingerprint(completed.body)
  );
}
export function clearAcceptedPortableDraft(
  owner: string,
  completed: PortableSubmission,
): boolean {
  try {
    const current = loadPortableDraft(owner);
    if (
      !current ||
      !portableCompletionIsCurrent(completed, {
        ...current,
        active: true,
        owner,
        requestOwner: owner,
      })
    )
      return false;
    sessionStorage.removeItem(storageKey(owner));
    return true;
  } catch {
    return false;
  }
}
export function portablePreflightMatches(
  body: PortableForm,
  preflight: PortablePreflight | null,
) {
  return (
    preflight?.result.valid === true &&
    preflight.fingerprint === portableFingerprint(body)
  );
}
export function restoreMatchingPortableDraft(
  owner: string,
  initial: PortableForm,
) {
  const saved = loadPortableDraft(owner);
  // A newly selected server configuration wins even if storage could not replace
  // an older draft. Never silently attach an old retry key to different inputs.
  return saved &&
    portableFingerprint(saved.body) === portableFingerprint(initial)
    ? saved
    : null;
}
export function usePortableDraft(owner: string, initial: PortableForm) {
  const restored = useRef(restoreMatchingPortableDraft(owner, initial));
  const [body, setBody] = useState(restored.current?.body ?? initial);
  const value = useRef(body),
    pending = useRef(restored.current?.pending ?? null),
    active = useRef(true),
    lock = useRef(false);
  const identity = useRef(owner);
  identity.current = owner;
  const [busy, setBusy] = useState(false),
    [hasPending, setHasPending] = useState(Boolean(pending.current)),
    [error, setError] = useState<unknown>(null),
    [storageWarning, setStorageWarning] = useState(false);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  function edit(next: PortableForm) {
    // Retain old pending envelope only when it belongs to exactly these values.
    if (portableFingerprint(next) !== portableFingerprint(value.current))
      pending.current = null;
    value.current = next;
    setBody(next);
    setHasPending(Boolean(pending.current));
    setError(null);
    setStorageWarning(
      !savePortableDraft(owner, {
        version: 1,
        body: next,
        pending: pending.current,
      }),
    );
  }
  async function submit<R>(
    perform: (body: PortableForm, key: string) => Promise<R>,
    accepted: (result: R) => void,
  ) {
    if (lock.current || !active.current || identity.current !== owner) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      const request = portableSubmissionFor(value.current, pending.current);
      pending.current = request;
      setHasPending(true);
      if (
        !savePortableDraft(owner, {
          version: 1,
          body: value.current,
          pending: request,
        })
      )
        throw new Error(
          "This browser cannot save the portable retry key. Enable tab storage before submitting; no request was sent.",
        );
      const result = await perform(request.body, request.key);
      if (
        portableCompletionIsCurrent(request, {
          active: active.current,
          owner: identity.current,
          requestOwner: owner,
          body: value.current,
          pending: pending.current,
        })
      ) {
        clearAcceptedPortableDraft(owner, request);
        pending.current = null;
        setHasPending(false);
        accepted(result);
      }
    } catch (failure) {
      if (active.current && identity.current === owner) setError(failure);
    } finally {
      lock.current = false;
      if (active.current && identity.current === owner) setBusy(false);
    }
  }
  return { body, edit, submit, busy, hasPending, error, storageWarning };
}
