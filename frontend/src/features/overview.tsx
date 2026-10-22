import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  Braces,
  CheckCheck,
  Database,
  FlaskConical,
  Plus,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  ErrorState,
  Loading,
  PageHeader,
  Panel,
  Status,
} from "@/components/common";
import { datasetsOptions, runsOptions, strategiesOptions } from "@/lib/api";
import {
  formatDate,
  shortId,
  taskLabels,
  terminalStatuses,
} from "@/lib/format";
import type { RunPublic } from "@/client/types.gen";

export function RunsTable({ runs }: { runs: RunPublic[] }) {
  if (!runs.length)
    return (
      <div className="empty">
        <FlaskConical size={30} />
        <h3>Your first experiment starts here</h3>
        <p>
          Choose a synthetic snapshot and an unchanged seed.
          <br />
          Every run keeps its own inputs, events, and artifacts.
        </p>
        <Button asChild variant="outline">
          <Link to="/runs/new">
            <Plus />
            New seed evaluation
          </Link>
        </Button>
      </div>
    );
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Experiment</th>
            <th>Status</th>
            <th>Created · UTC</th>
            <th>
              <span className="sr-only">Open run</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>
                <Link
                  to="/runs/$runId"
                  params={{ runId: run.id }}
                  className="cell-main"
                >
                  {taskLabels[String(run.config.task)] || "Seed evaluation"}
                </Link>
                <span className="cell-sub">
                  {shortId(run.id)} · Attempt generation {run.generation}
                </span>
              </td>
              <td>
                <Status status={run.status} />
              </td>
              <td className="muted whitespace-nowrap">
                {formatDate(run.created_at)}
              </td>
              <td>
                <Link
                  to="/runs/$runId"
                  params={{ runId: run.id }}
                  aria-label={`Open experiment ${shortId(run.id)}`}
                  className="text-link"
                >
                  <ArrowRight size={15} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function OverviewPage() {
  const runs = useQuery(runsOptions());
  const datasets = useQuery(datasetsOptions);
  const strategies = useQuery(strategiesOptions);
  if (runs.isPending || datasets.isPending || strategies.isPending)
    return <Loading />;
  if (runs.error || datasets.error || strategies.error)
    return (
      <ErrorState
        error={runs.error || datasets.error || strategies.error}
        retry={() => {
          void runs.refetch();
          void datasets.refetch();
          void strategies.refetch();
        }}
      />
    );
  const recent = runs.data?.data || [];
  const stats = [
    {
      label: "Active in recent runs",
      value: recent.filter((run) => !terminalStatuses.has(run.status)).length,
      note: "Durable queue & execution",
      icon: Activity,
    },
    {
      label: "Dataset snapshots",
      value: datasets.data?.count ?? 0,
      note: "Immutable · synthetic demo",
      icon: Database,
    },
    {
      label: "Seed strategies",
      value: strategies.data?.count ?? 0,
      note: "Original code · unchanged",
      icon: Braces,
    },
    {
      label: "Completed in recent runs",
      value: recent.filter((run) => run.status === "succeeded").length,
      note: "Fixed-seed evaluations",
      icon: CheckCheck,
    },
  ];
  return (
    <>
      <PageHeader
        title="Research overview"
        subtitle="A clear view of your data, experiments, and evidence."
      >
        <Button asChild>
          <Link to="/runs/new">
            <Plus />
            New experiment
          </Link>
        </Button>
      </PageHeader>
      <section className="hero">
        <div>
          <div className="eyebrow">YOUR RESEARCH, TRACEABLE BY DESIGN</div>
          <h2>From a fixed seed to a reproducible result.</h2>
          <p>
            Start with synthetic data, run one of the four original strategies,
            and follow every stage. Your inputs and results stay linked, even
            after a reload.
          </p>
        </div>
        <div className="hero-tag">
          <FlaskConical size={25} />
          <strong className="block mt-3 text-sm font-medium">
            M1 foundation
          </strong>
          <span>Seed-only · no model calls</span>
        </div>
      </section>
      <div className="stats">
        {stats.map(({ label, value, note, icon: Icon }) => (
          <div className="stat" key={label}>
            <div className="stat-head">
              {label}
              <Icon />
            </div>
            <strong className="stat-value">{value}</strong>
            <span className="stat-note">{note}</span>
          </div>
        ))}
      </div>
      <div className="section-grid">
        <Panel
          title="Recent experiments"
          body={false}
          action={
            <Link to="/runs" className="text-link">
              View all
              <ArrowRight size={13} />
            </Link>
          }
        >
          <RunsTable runs={recent.slice(0, 6)} />
        </Panel>
        <div className="stack">
          <Panel title="In this foundation">
            <div className="flex items-start gap-3">
              <div className="catalog-icon !mb-0 !h-9 !w-9 shrink-0">
                <Sparkles size={17} />
              </div>
              <div>
                <h3>Small scope. Real plumbing.</h3>
                <p className="mt-2 text-xs leading-6 muted">
                  Authenticated runs, durable orchestration, isolated seed
                  evaluation, and permission-checked artifacts.
                </p>
              </div>
            </div>
            <div className="roadmap-row">
              <span className="roadmap-mark">M2</span>
              <div>
                <h3 className="!text-xs">Market-data adapters</h3>
                <p>
                  AKShare, yfinance, and OpenBB are not enabled in this build.
                </p>
              </div>
            </div>
            <div className="roadmap-row">
              <span className="roadmap-mark">M3</span>
              <div>
                <h3 className="!text-xs">TradeGrad optimization</h3>
                <p>
                  Candidate evolution and optimizer checkpoints come later.
                  Current resumes restart the fixed seed.
                </p>
              </div>
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}
export function RunsPage() {
  const runs = useQuery(runsOptions());
  return (
    <>
      <PageHeader
        title="Experiments"
        subtitle="Each evaluation has a fixed configuration and a durable history."
      >
        <Button asChild>
          <Link to="/runs/new">
            <Plus />
            New experiment
          </Link>
        </Button>
      </PageHeader>
      {runs.isPending ? (
        <Loading />
      ) : runs.error ? (
        <ErrorState error={runs.error} retry={() => void runs.refetch()} />
      ) : (
        <Panel
          title={`Recent experiments · ${runs.data?.count ?? 0} total`}
          body={false}
        >
          <RunsTable runs={runs.data?.data || []} />
        </Panel>
      )}
      <p className="mt-4 text-xs muted">
        Showing up to 100 recent experiments. All dates and event times are UTC.
      </p>
    </>
  );
}
