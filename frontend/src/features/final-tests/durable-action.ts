import { useEffect, useRef, useState } from "react";
import { z } from "zod";
import { stableJson } from "@/lib/data-utils";

export type PendingAction<T> = { key: string; body: T };
export type ActionDraft<T> = {
  version: 1;
  body: T | null;
  pending: PendingAction<T> | null;
};
const keyFor = (owner: string, scope: string) =>
  `tradegrad.final-draft.v1.${encodeURIComponent(owner)}.${encodeURIComponent(scope)}`;
export function actionFor<T>(
  body: T,
  previous: PendingAction<T> | null,
): PendingAction<T> {
  return previous && stableJson(previous.body) === stableJson(body)
    ? previous
    : { key: crypto.randomUUID(), body: structuredClone(body) };
}
export function loadActionDraft<T>(
  owner: string,
  scope: string,
  schema: z.ZodType<T>,
): ActionDraft<T> | null {
  try {
    const parsed = z
      .strictObject({
        version: z.literal(1),
        body: schema.nullable(),
        pending: z.strictObject({ key: z.uuid(), body: schema }).nullable(),
      })
      .safeParse(
        JSON.parse(sessionStorage.getItem(keyFor(owner, scope)) || "null"),
      );
    if (!parsed.success) return null;
    // A stale/malformed draft may retain an older envelope after edits. It is
    // never permission to replay the edited body without a fresh preflight.
    if (
      parsed.data.pending &&
      stableJson(parsed.data.body) !== stableJson(parsed.data.pending.body)
    )
      parsed.data.pending = null;
    return parsed.data;
  } catch {
    return null;
  }
}
export function saveActionDraft<T>(
  owner: string,
  scope: string,
  schema: z.ZodType<T>,
  draft: ActionDraft<T>,
): boolean {
  try {
    const parsed = z
      .strictObject({
        version: z.literal(1),
        body: schema.nullable(),
        pending: z.strictObject({ key: z.uuid(), body: schema }).nullable(),
      })
      .safeParse(draft);
    if (!parsed.success) return false;
    sessionStorage.setItem(keyFor(owner, scope), JSON.stringify(parsed.data));
    return true;
  } catch {
    return false;
  }
}
/** Invalid edits still durably invalidate a previous pending request. */
export function saveEditedActionDraft<T>(
  owner: string,
  scope: string,
  schema: z.ZodType<T>,
  body: T | null,
  pending: PendingAction<T> | null,
): boolean {
  if (body !== null && !schema.safeParse(body).success)
    return saveActionDraft(owner, scope, schema, {
      version: 1,
      body: null,
      pending: null,
    });
  return saveActionDraft(owner, scope, schema, { version: 1, body, pending });
}
export function actionCompletionIsCurrent<T>(
  completed: PendingAction<T>,
  current: {
    active: boolean;
    body: T | null;
    pending: PendingAction<T> | null;
  },
) {
  return (
    current.active &&
    current.pending?.key === completed.key &&
    stableJson(current.body) === stableJson(completed.body) &&
    stableJson(current.pending.body) === stableJson(completed.body)
  );
}
export function clearAcceptedActionDraft<T>(
  owner: string,
  scope: string,
  schema: z.ZodType<T>,
  completed: PendingAction<T>,
): boolean {
  const current = loadActionDraft(owner, scope, schema);
  if (
    !current ||
    !actionCompletionIsCurrent(completed, { ...current, active: true })
  )
    return false;
  try {
    sessionStorage.removeItem(keyFor(owner, scope));
    return true;
  } catch {
    return false;
  }
}

/** A retry reuses the exact persisted body and key. Mounts and owners never share mutable state. */
export function useDurableAction<T>({
  owner,
  scope,
  schema,
  initial,
}: {
  owner: string;
  scope: string;
  schema: z.ZodType<T>;
  initial: T | null;
}) {
  const restored = useRef(loadActionDraft(owner, scope, schema));
  const [body, setValue] = useState<T | null>(
    restored.current?.body ?? initial,
  );
  const value = useRef(body);
  const pending = useRef(restored.current?.pending ?? null);
  const [hasPending, setHasPending] = useState(Boolean(pending.current));
  const active = useRef(true);
  const locked = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [storageWarning, setStorageWarning] = useState(false);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const persist = () =>
    saveActionDraft(owner, scope, schema, {
      version: 1,
      body: value.current,
      pending: pending.current,
    });
  function edit(next: T | null) {
    if (stableJson(next) !== stableJson(value.current)) pending.current = null;
    value.current = next;
    setValue(next);
    setHasPending(Boolean(pending.current));
    setError(null);
    setStorageWarning(
      !saveEditedActionDraft(owner, scope, schema, next, pending.current),
    );
  }
  async function run<R>(
    perform: (body: T, key: string) => Promise<R>,
    accepted: (result: R) => void,
  ) {
    if (locked.current || !active.current) return;
    const parsed = schema.safeParse(value.current);
    if (!parsed.success) {
      setError(new Error("Choose complete, valid inputs before submitting."));
      return;
    }
    locked.current = true;
    setBusy(true);
    setError(null);
    const request = actionFor(parsed.data, pending.current);
    pending.current = request;
    setHasPending(true);
    try {
      if (!persist())
        throw new Error(
          "This browser cannot save the retry key. Enable tab storage before submitting; no request was sent.",
        );
      const result = await perform(request.body, request.key);
      if (
        actionCompletionIsCurrent(request, {
          active: active.current,
          body: value.current,
          pending: pending.current,
        })
      ) {
        clearAcceptedActionDraft(owner, scope, schema, request);
        pending.current = null;
        setHasPending(false);
        accepted(result);
      }
    } catch (failure) {
      if (active.current) setError(failure);
    } finally {
      locked.current = false;
      if (active.current) setBusy(false);
    }
  }
  return { body, edit, run, busy, error, hasPending, storageWarning };
}
