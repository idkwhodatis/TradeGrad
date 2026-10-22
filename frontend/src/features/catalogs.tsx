import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Braces, Code2, Plus, X } from "lucide-react";
import type {
  StrategyCreate,
  StrategyDetail,
  StrategyPublic,
} from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ErrorState, Loading, PageHeader, Panel } from "@/components/common";
import { CodeDiff, CodeEditor, CodeViewer } from "@/components/code-editor";
import { currentUserOptions, queryClient, strategyOptions } from "@/lib/api";
import {
  STRATEGY_PAGE_SIZE,
  saveStrategyVersion,
  strategyPageOptions,
} from "@/lib/strategy-api";
import {
  clearStrategyDraft,
  emptyStrategy,
  isDefinitiveStrategyFailure,
  loadStrategyDraft,
  MAX_STRATEGY_BYTES,
  readStrategyUpload,
  saveStrategyDraft,
  strategyBytes,
  strategyInputError,
  strategyOrigin,
  strategySubmissionFor,
  strategyTasks,
  type StrategyDraft,
} from "@/lib/strategy-versions";
import { taskLabels } from "@/lib/format";

export function StrategiesPage() {
  const user = useQuery(currentUserOptions);
  return (
    <>
      <PageHeader
        title="Strategy library"
        subtitle="Choose a seed, upload Python, or derive a new native version. Saved source is never edited in place."
      />
      <p className="notice">
        Native AKQuant source only. Saving parses Python syntax; it does not
        execute code, validate isolation, prove safety, or certify a portable
        strategy. Run evaluations separately in the configured isolated runtime.
      </p>
      <div className="mt-6">
        {user.isPending ? (
          <Loading />
        ) : user.error ? (
          <ErrorState error={user.error} retry={() => void user.refetch()} />
        ) : user.data ? (
          <StrategyWorkspace key={user.data.id} owner={user.data.id} />
        ) : null}
      </div>
    </>
  );
}

function StrategyWorkspace({ owner }: { owner: string }) {
  const [offset, setOffset] = useState(0);
  const strategies = useQuery(strategyPageOptions(owner, offset));
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<StrategyDraft | null>(() =>
    loadStrategyDraft(owner),
  );
  const [editing, setEditing] = useState(() => !!loadStrategyDraft(owner));
  const [replace, setReplace] = useState<StrategyCreate | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [storageError, setStorageError] = useState<unknown>(null);
  function updateDraft(value: StrategyDraft) {
    try {
      saveStrategyDraft(owner, value);
      setStorageError(null);
    } catch {
      throw new Error(
        "This tab could not persist the draft and retry key. Free browser storage or allow session storage before saving.",
      );
    }
    setDraft(value);
  }
  function begin(body: StrategyCreate) {
    if (draft) {
      setReplace(body);
      setEditing(true);
      return;
    }
    try {
      updateDraft({ body, pending: null });
      setEditing(true);
      setNotice(null);
    } catch (error) {
      setStorageError(error);
    }
  }
  function discard() {
    try {
      clearStrategyDraft(owner);
      setDraft(null);
      setEditing(false);
      setReplace(null);
      setStorageError(null);
    } catch {
      setStorageError(
        new Error("The local draft could not be removed from this tab."),
      );
    }
  }
  function saved(item: StrategyDetail) {
    // The network response established success. A storage cleanup failure must
    // never turn this acknowledged write into a new submission.
    try {
      clearStrategyDraft(owner);
    } catch {
      setStorageError(
        new Error(
          "Version saved, but the local retry draft could not be cleared. Replaying it is safe and returns the same version.",
        ),
      );
    }
    setDraft(null);
    setEditing(false);
    setReplace(null);
    setSelected(item.id);
    setOffset(0);
    setNotice(
      `Saved ${item.name} as a new immutable version. Python syntax parsed only; evaluation has not run.`,
    );
    queryClient.setQueryData(strategyOptions(item.id).queryKey, item);
    void queryClient.invalidateQueries({ queryKey: ["strategies"] });
  }
  return (
    <>
      <div className="flex gap-3 mb-5">
        <Button
          onClick={() => (draft ? setEditing(true) : begin(emptyStrategy()))}
        >
          <Plus />
          {draft ? "Continue local draft" : "Upload or create strategy"}
        </Button>
        {draft && !draft.pending && (
          <Button variant="outline" onClick={() => setReplace(emptyStrategy())}>
            Discard local draft
          </Button>
        )}
      </div>
      {notice && (
        <p className="notice mb-5" role="status">
          {notice}
        </p>
      )}
      {storageError != null && <ErrorState error={storageError} />}
      {replace && (
        <div className="notice mb-5" role="alert">
          <div>
            <p>
              {draft?.pending
                ? "An earlier save has an uncertain outcome. Retry the exact saved request before replacing the draft; the version may already exist."
                : "You already have a local draft. Replacing it discards unsaved source, not any saved version."}
            </p>
            <div className="flex gap-3 mt-3">
              <Button
                variant="outline"
                onClick={() => {
                  setReplace(null);
                  setEditing(true);
                }}
              >
                Keep current draft
              </Button>
              {!draft?.pending && (
                <Button
                  onClick={() => {
                    const next = replace;
                    discard();
                    if (next.code || next.name) beginReplacement(next);
                  }}
                >
                  Discard and continue
                </Button>
              )}
            </div>
          </div>
        </div>
      )}
      {editing && draft && (
        <div className="mb-6">
          <StrategyForm
            owner={owner}
            draft={draft}
            onChange={updateDraft}
            onClose={() => setEditing(false)}
            onSaved={saved}
          />
        </div>
      )}
      {selected && (
        <div className="mb-6">
          <StrategySource
            id={selected}
            onClose={() => setSelected(null)}
            onEdit={(source) =>
              begin({
                name: `${source.name.slice(0, 235)} (new version)`,
                task: source.task,
                code: source.code,
                parent_id: source.id,
              })
            }
          />
        </div>
      )}
      {strategies.isPending ? (
        <Loading />
      ) : strategies.error ? (
        <ErrorState
          error={strategies.error}
          retry={() => void strategies.refetch()}
        />
      ) : (
        <>
          {!strategies.data?.data.length && (
            <p className="notice">
              No strategy versions on this page. Upload a Python file or create
              a new native strategy.
            </p>
          )}
          <div className="catalog">
            {strategies.data?.data.map((strategy) => (
              <StrategyCard
                key={strategy.id}
                strategy={strategy}
                selected={selected === strategy.id}
                onSelect={() =>
                  setSelected(selected === strategy.id ? null : strategy.id)
                }
              />
            ))}
          </div>
          <div
            className="flex gap-3 items-center mt-5"
            aria-label="Strategy pages"
          >
            <Button
              variant="outline"
              disabled={offset === 0}
              onClick={() =>
                setOffset(Math.max(0, offset - STRATEGY_PAGE_SIZE))
              }
            >
              Previous strategies
            </Button>
            <span className="text-sm muted">
              {strategies.data?.count || 0} versions · page{" "}
              {Math.floor(offset / STRATEGY_PAGE_SIZE) + 1}
            </span>
            <Button
              variant="outline"
              disabled={
                offset + STRATEGY_PAGE_SIZE >= (strategies.data?.count || 0)
              }
              onClick={() => setOffset(offset + STRATEGY_PAGE_SIZE)}
            >
              Next strategies
            </Button>
          </div>
        </>
      )}
    </>
  );
  function beginReplacement(body: StrategyCreate) {
    try {
      updateDraft({ body, pending: null });
      setEditing(true);
    } catch (error) {
      setStorageError(error);
    }
  }
}

function StrategyCard({
  strategy,
  selected,
  onSelect,
}: {
  strategy: StrategyPublic;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <article className="panel panel-body">
      <div className="flex items-start justify-between">
        <div className="catalog-icon">
          <Braces size={21} />
        </div>
        <Badge variant="outline">{strategy.mode}</Badge>
      </div>
      <h2>{strategy.name}</h2>
      <p className="mt-2 text-xs muted">
        {taskLabels[strategy.task]} · {strategyOrigin(strategy)}
      </p>
      <dl className="details">
        <div>
          <dt>Parent version</dt>
          <dd className="hash">{strategy.parent_id || "No parent recorded"}</dd>
        </div>
        <div>
          <dt>Code SHA-256</dt>
          <dd className="hash">{strategy.content_sha256}</dd>
        </div>
      </dl>
      <Button variant="outline" size="sm" className="mt-5" onClick={onSelect}>
        <Code2 />
        {selected ? "Hide source" : "View source"}
      </Button>
    </article>
  );
}

function StrategySource({
  id,
  onClose,
  onEdit,
}: {
  id: string;
  onClose: () => void;
  onEdit: (strategy: StrategyDetail) => void;
}) {
  const detail = useQuery(strategyOptions(id));
  const [compare, setCompare] = useState(false);
  const parentId = detail.data?.parent_id;
  const parent = useQuery({
    ...strategyOptions(parentId || ""),
    enabled: compare && !!parentId,
  });
  return (
    <Panel
      title={detail.data?.name || "Strategy source"}
      action={
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Close source"
          onClick={onClose}
        >
          <X />
        </Button>
      }
    >
      {detail.isPending ? (
        <Loading text="Loading strategy source…" />
      ) : detail.error ? (
        <ErrorState error={detail.error} retry={() => void detail.refetch()} />
      ) : detail.data ? (
        <>
          <p className="muted text-sm mb-4">
            Read-only saved version · {detail.data.id}
          </p>
          <div className="flex gap-3 mb-4">
            <Button onClick={() => onEdit(detail.data!)}>
              Edit as new version
            </Button>
            {parentId && (
              <Button variant="outline" onClick={() => setCompare(!compare)}>
                {compare ? "Show source" : "Compare with parent"}
              </Button>
            )}
          </div>
          {compare && parentId ? (
            parent.isPending ? (
              <Loading text="Loading parent source…" />
            ) : parent.error ? (
              <ErrorState
                error={parent.error}
                retry={() => void parent.refetch()}
              />
            ) : parent.data ? (
              <CodeDiff
                original={parent.data.code}
                modified={detail.data.code}
                label="Saved version and parent comparison"
              />
            ) : null
          ) : (
            <CodeViewer
              code={detail.data.code}
              label={`${detail.data.name} source`}
            />
          )}
        </>
      ) : null}
    </Panel>
  );
}

function StrategyForm({
  owner,
  draft,
  onChange,
  onClose,
  onSaved,
}: {
  owner: string;
  draft: StrategyDraft;
  onChange: (draft: StrategyDraft) => void;
  onClose: () => void;
  onSaved: (item: StrategyDetail) => void;
}) {
  const submitting = useRef(false);
  const uploadGuard = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [compare, setCompare] = useState(false);
  const parentId = draft.body.parent_id;
  const parent = useQuery({
    ...strategyOptions(parentId || ""),
    enabled: compare && !!parentId,
  });
  const locked = busy || uploading || !!draft.pending;
  function edit(change: Partial<StrategyCreate>) {
    if (locked || uploadGuard.current || submitting.current) return;
    const body = { ...draft.body, ...change };
    if (strategyBytes(body.code) > MAX_STRATEGY_BYTES) {
      setError(
        new Error("Python source must not exceed 128 KiB of UTF-8 text."),
      );
      return;
    }
    try {
      onChange({ body, pending: null });
      setError(null);
    } catch (failure) {
      setError(failure);
    }
  }
  async function upload(file: File | undefined) {
    if (!file || locked || uploadGuard.current || submitting.current) return;
    uploadGuard.current = true;
    setUploading(true);
    setError(null);
    try {
      const code = await readStrategyUpload(file);
      if (!mounted.current) return;
      onChange({
        body: {
          ...draft.body,
          code,
          name:
            draft.body.name || file.name.replace(/\.py$/i, "").slice(0, 255),
        },
        pending: null,
      });
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      uploadGuard.current = false;
      if (mounted.current) setUploading(false);
    }
  }
  async function submit() {
    if (submitting.current || uploadGuard.current || uploading) return;
    const validation = strategyInputError(draft.body);
    if (validation) {
      setError(new Error(validation));
      return;
    }
    submitting.current = true;
    setBusy(true);
    setError(null);
    const pending = draft.pending || strategySubmissionFor(draft.body);
    let sent = false;
    try {
      onChange({ body: pending.body, pending });
      sent = true;
      const result = await saveStrategyVersion(pending);
      if (!mounted.current) return;
      if (!result)
        throw new Error(
          "The server returned no strategy confirmation. Retry the same submission.",
        );
      onSaved(result);
    } catch (failure) {
      if (!mounted.current) return;
      if (sent && isDefinitiveStrategyFailure(failure)) {
        try {
          onChange({ body: pending.body, pending: null });
        } catch {
          /* Keep the request frozen if tab storage becomes unavailable. */
        }
      }
      setError(failure);
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return (
    <Panel
      title="New immutable strategy version"
      action={
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Close draft"
          disabled={busy || uploading}
          onClick={onClose}
        >
          <X />
        </Button>
      }
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        aria-label="Create strategy version"
      >
        <p className="text-sm muted mb-4">
          Drafts stay in this tab for your account. Closing keeps the draft;
          saving creates a new native_akquant version. Original source and
          parent versions remain unchanged.
        </p>
        {draft.pending && (
          <p className="notice mb-4" role="status">
            {busy
              ? "Saving this exact version…"
              : "A save may already have succeeded. The source is frozen until you retry the same submission; reloads reuse the same retry key."}
          </p>
        )}
        <div className="grid gap-4 sm:grid-cols-2 mb-4">
          <label className="field">
            <span>Version name</span>
            <Input
              aria-label="Version name"
              value={draft.body.name}
              maxLength={255}
              disabled={locked}
              onChange={(event) => edit({ name: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Strategy task</span>
            <select
              aria-label="Strategy task"
              value={draft.body.task}
              disabled={locked || !!parentId}
              onChange={(event) =>
                edit({ task: event.target.value as StrategyCreate["task"] })
              }
            >
              {strategyTasks.map((task) => (
                <option key={task} value={task}>
                  {taskLabels[task]}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="text-xs muted mb-4">
          Parent: {parentId || "None (new uploaded or authored source)"}
          {parentId ? " · Derived versions keep the parent's task." : ""}
        </p>
        <label className="field mb-4">
          <span>Upload Python file (.py, UTF-8, maximum 128 KiB)</span>
          <Input
            type="file"
            accept=".py,text/x-python"
            aria-label="Upload Python file"
            disabled={locked}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              void upload(file);
            }}
          />
        </label>
        <CodeEditor
          code={draft.body.code}
          disabled={locked}
          onChange={(code) => edit({ code })}
        />
        <p className="text-xs muted mt-2">
          {strategyBytes(draft.body.code).toLocaleString()} /{" "}
          {MAX_STRATEGY_BYTES.toLocaleString()} UTF-8 bytes · exact submitted
          source determines the SHA-256.
        </p>
        {parentId && (
          <div className="mt-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => setCompare(!compare)}
            >
              {compare ? "Hide draft comparison" : "Compare draft with parent"}
            </Button>
            {compare &&
              (parent.isPending ? (
                <Loading />
              ) : parent.error ? (
                <ErrorState
                  error={parent.error}
                  retry={() => void parent.refetch()}
                />
              ) : parent.data ? (
                <CodeDiff
                  original={parent.data.code}
                  modified={draft.body.code}
                  label="Draft and immutable parent comparison"
                />
              ) : null)}
          </div>
        )}
        {error != null && (
          <div className="mt-4">
            <ErrorState error={error} />
            <SyntaxDetail error={error} />
          </div>
        )}
        <div className="flex gap-3 mt-5">
          <Button type="submit" disabled={busy || uploading}>
            {busy
              ? "Saving…"
              : draft.pending
                ? "Retry same save"
                : "Save new version"}
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={busy || uploading}
            onClick={onClose}
          >
            Close and keep draft
          </Button>
        </div>
        <span className="sr-only">Draft owner {owner}</span>
      </form>
    </Panel>
  );
}
function SyntaxDetail({ error }: { error: unknown }) {
  const value = error as {
    code?: string;
    details?: { line?: number; column?: number; reason?: string };
  };
  return value?.code === "strategy_syntax_error" && value.details ? (
    <p className="text-sm mt-2" role="alert">
      Line {value.details.line ?? "unknown"}, column{" "}
      {value.details.column ?? "unknown"}:{" "}
      {value.details.reason || "Source could not be parsed."}
    </p>
  ) : null;
}
