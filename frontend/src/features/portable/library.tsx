import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import * as sdk from "@/client/sdk.gen";
import type {
  PortableSourceCreate,
  PortableStrategyDetail,
} from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CodeDiff, CodeEditor, CodeViewer } from "@/components/code-editor";
import { ErrorState, Panel } from "@/components/common";
import { queryClient } from "@/lib/api";
import { stableJson } from "@/lib/data-utils";
import {
  isDefinitiveStrategyFailure,
  readStrategyUpload,
  strategyInputError,
  strategyTasks,
} from "@/lib/strategy-versions";

type Draft = {
  name: string;
  task: PortableSourceCreate["task"];
  code: string;
  parent_id: string | null;
  manifest: string;
  parameters: string;
  pending: { key: string; body: PortableSourceCreate } | null;
};
const blank = (): Draft => ({
  name: "",
  task: "cn_time",
  code: "",
  parent_id: null,
  manifest: "{}",
  parameters: "{}",
  pending: null,
});
const keyFor = (owner: string) => `tradegrad.portable-source-draft.v1.${owner}`;
function restore(owner: string): Draft | null {
  try {
    const text = sessionStorage.getItem(keyFor(owner));
    if (!text || text.length > 2_000_000) return null;
    const value = JSON.parse(text) as Draft;
    if (
      !value ||
      !strategyTasks.includes(value.task) ||
      typeof value.name !== "string" ||
      typeof value.code !== "string" ||
      typeof value.manifest !== "string" ||
      typeof value.parameters !== "string"
    )
      return null;
    if (value.pending) {
      if (
        typeof value.pending.key !== "string" ||
        !/^[!-~]{1,128}$/.test(value.pending.key) ||
        !value.pending.body
      )
        return null;
      const exact = {
        name: value.name,
        task: value.task,
        code: value.code,
        parent_id: value.parent_id,
        manifest: JSON.parse(value.manifest),
        parameters: JSON.parse(value.parameters),
      };
      if (stableJson(exact) !== stableJson(value.pending.body)) return null;
    }
    return value;
  } catch {
    return null;
  }
}

export function PortableLibrary({ owner }: { owner: string }) {
  const [offset, setOffset] = useState(0);
  const list = useQuery({
    queryKey: ["portable-source", owner, offset],
    queryFn: async ({ signal }) =>
      (
        await sdk.listPortableStrategies({
          query: { offset, limit: 25 },
          signal,
        })
      ).data,
  });
  const [selected, setSelected] = useState<PortableStrategyDetail | null>(null);
  const [draft, setDraft] = useState<Draft | null>(() => restore(owner));
  const [editing, setEditing] = useState(() => !!restore(owner));
  const parent = useQuery({
    queryKey: ["portable-source-parent", owner, draft?.parent_id],
    enabled: !!draft?.parent_id,
    queryFn: async ({ signal }) =>
      (
        await sdk.readPortableStrategy({
          path: { strategy_id: draft!.parent_id! },
          signal,
        })
      ).data,
  });
  const parentCode = parent.data?.code ?? null;
  const [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  function persist(value: Draft) {
    sessionStorage.setItem(keyFor(owner), JSON.stringify(value));
    setDraft(value);
  }
  function edit(change: Partial<Draft>) {
    if (!draft || lock.current || draft.pending) return;
    try {
      persist({ ...draft, ...change });
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  async function perform(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (failure) {
      setError(failure);
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  function begin() {
    if (draft) {
      setEditing(true);
      return;
    }
    try {
      persist(blank());
      setEditing(true);
    } catch (failure) {
      setError(failure);
    }
  }
  async function derive() {
    if (!selected || draft) return;
    const value = (
      await sdk.portableSourceDraft({ path: { strategy_id: selected.id } })
    ).data;
    persist({
      name: value.name,
      task: value.task,
      code: value.code,
      parent_id: selected.id,
      manifest: JSON.stringify(value.manifest, null, 2),
      parameters: JSON.stringify(value.parameters, null, 2),
      pending: null,
    });
    setEditing(true);
  }
  async function save() {
    if (!draft) return;
    const body: PortableSourceCreate = draft.pending?.body ?? {
      name: draft.name,
      task: draft.task,
      code: draft.code,
      parent_id: draft.parent_id,
      manifest: JSON.parse(draft.manifest),
      parameters: JSON.parse(draft.parameters),
    };
    const invalid = strategyInputError({
      name: body.name,
      task: body.task,
      code: body.code,
    });
    if (invalid) throw new Error(invalid);
    const pending = draft.pending ?? { key: crypto.randomUUID(), body };
    persist({ ...draft, pending });
    let item: PortableStrategyDetail;
    try {
      item = (
        await sdk.createPortableSourceVersion({
          body: pending.body,
          headers: { "Idempotency-Key": pending.key },
        })
      ).data;
    } catch (failure) {
      const code = (failure as { code?: string })?.code;
      if (
        isDefinitiveStrategyFailure(failure) ||
        code === "portable_declaration_invalid" ||
        code === "portable_declaration_required"
      )
        persist({ ...draft, pending: null });
      throw failure;
    }
    setSelected(item);
    setDraft(null);
    setEditing(false);
    setOffset(0);
    setNotice(
      "Saved new immutable portable version. Declarations and syntax checked only; no policy execution or certification occurred.",
    );
    try {
      sessionStorage.removeItem(keyFor(owner));
    } catch {
      setNotice(
        "Version saved. The retry draft could not be removed; retrying it returns the same version.",
      );
    }
    await queryClient.invalidateQueries({ queryKey: ["portable-source"] });
    await queryClient.invalidateQueries({ queryKey: ["portable"] });
  }
  return (
    <div className="mt-8">
      <Panel title="Portable strategy versions">
        <p className="notice mb-4">
          Upload or edit a complete Policy source, not only a reference
          template. The strict manifest, bounded typed parameters and explicit
          initial state are user declarations, not proof of safe code. Execution
          still requires the fail-closed paired-container runtime. Optimization
          requires one EDIT START / EDIT END region and preserves the rest of
          your seed source; zero-iteration evaluation also accepts source
          without markers.
        </p>
        <Button onClick={begin} disabled={busy}>
          {draft
            ? "Continue portable draft"
            : "Upload or create portable policy"}
        </Button>
        {draft && !draft.pending && (
          <Button
            variant="outline"
            className="ml-3"
            disabled={busy}
            onClick={() => {
              try {
                sessionStorage.removeItem(keyFor(owner));
                setDraft(null);
                setEditing(false);
              } catch (failure) {
                setError(failure);
              }
            }}
          >
            Discard portable draft
          </Button>
        )}
        {notice && (
          <p role="status" className="notice mt-4">
            {notice}
          </p>
        )}
        {error != null && <ErrorState error={error} />}
        {editing && draft && (
          <form
            className="stack mt-5"
            aria-label="Create portable source version"
            onSubmit={(event) => {
              event.preventDefault();
              void perform(save);
            }}
          >
            {draft.pending && (
              <p className="notice">
                A save may already have succeeded. This exact request is frozen;
                retry it with the same key.
              </p>
            )}
            <label className="field">
              <span>Portable version name</span>
              <Input
                aria-label="Portable version name"
                value={draft.name}
                disabled={busy || !!draft.pending}
                onChange={(event) => edit({ name: event.target.value })}
              />
            </label>
            <label className="field">
              <span>Portable task</span>
              <select
                aria-label="Portable task"
                value={draft.task}
                disabled={busy || !!draft.pending || !!draft.parent_id}
                onChange={(event) =>
                  edit({ task: event.target.value as Draft["task"] })
                }
              >
                {strategyTasks.map((task) => (
                  <option key={task}>{task}</option>
                ))}
              </select>
            </label>
            <p className="muted">
              Parent: {draft.parent_id ?? "None"}. Server assigns new
              strategy/code/parameter identities and clears any reference-result
              claim.
            </p>
            <label className="field">
              <span>Upload portable Python file</span>
              <Input
                type="file"
                accept=".py"
                aria-label="Upload portable Python file"
                disabled={busy || !!draft.pending}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file)
                    void perform(async () => {
                      const code = await readStrategyUpload(file);
                      persist({ ...draft, code });
                    });
                }}
              />
            </label>
            <CodeEditor
              label="Portable Python source"
              code={draft.code}
              disabled={busy || !!draft.pending}
              onChange={(code) => edit({ code })}
            />
            <label className="field">
              <span>Portable manifest JSON</span>
              <textarea
                aria-label="Portable manifest JSON"
                rows={12}
                value={draft.manifest}
                disabled={busy || !!draft.pending}
                onChange={(event) => edit({ manifest: event.target.value })}
              />
            </label>
            <p className="muted">
              Use “Edit portable as new version” on an existing version to
              obtain a complete editable declaration, or paste your full
              portable.v1 StrategyManifest with authoring.parameter_schema and
              checksum-verified authoring.initial_state.
            </p>
            <label className="field">
              <span>Portable parameter JSON</span>
              <textarea
                aria-label="Portable parameter JSON"
                rows={5}
                value={draft.parameters}
                disabled={busy || !!draft.pending}
                onChange={(event) => edit({ parameters: event.target.value })}
              />
            </label>
            {parentCode !== null && (
              <CodeDiff
                original={parentCode}
                modified={draft.code}
                label="Portable draft and parent comparison"
              />
            )}
            <div className="flex gap-3">
              <Button type="submit" disabled={busy}>
                {draft.pending
                  ? "Retry portable save"
                  : "Save portable version"}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => setEditing(false)}
              >
                Close and keep portable draft
              </Button>
            </div>
          </form>
        )}
        {list.error && <ErrorState error={list.error} />}
        <div className="stack mt-5">
          {list.data?.data.map((item) => (
            <Button
              key={item.id}
              variant="outline"
              disabled={busy}
              onClick={() =>
                void perform(async () => {
                  setSelected(
                    (
                      await sdk.readPortableStrategy({
                        path: { strategy_id: item.id },
                      })
                    ).data,
                  );
                })
              }
            >
              View portable {item.name}
            </Button>
          ))}
        </div>
        <div className="flex gap-3 mt-3">
          <Button
            variant="outline"
            disabled={busy || offset === 0}
            onClick={() => setOffset(offset - 25)}
          >
            Previous portable versions
          </Button>
          <Button
            variant="outline"
            disabled={busy || offset + 25 >= (list.data?.count ?? 0)}
            onClick={() => setOffset(offset + 25)}
          >
            Next portable versions
          </Button>
        </div>
        {selected && (
          <div className="stack mt-5">
            <h3>{selected.name}</h3>
            <p className="hash">
              Version {selected.id} · Parent {selected.parent_id ?? "None"} ·
              SHA-256 {selected.content_sha256}
            </p>
            <details>
              <summary>
                Portable declared capabilities, state and parameters
              </summary>
              <pre className="overflow-auto">
                {JSON.stringify(
                  {
                    manifest: selected.manifest,
                    parameters: selected.parameters,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
            <CodeViewer code={selected.code} label="Saved portable source" />
            <Button
              disabled={busy || !!draft}
              onClick={() => void perform(derive)}
            >
              Edit portable as new version
            </Button>
            {draft && (
              <p className="muted">
                Finish or discard the current portable draft before deriving
                another version.
              </p>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}
