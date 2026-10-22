import type { ReactNode } from "react";
import { AlertCircle, LoaderCircle, FlaskConical } from "lucide-react";
import { Button } from "@/components/ui/button";
import { describeError } from "@/lib/api";
import { label } from "@/lib/format";
import { nativeWindowWarning } from "@/lib/execution-notices";

export function NativeWindowNotice() {
  return (
    <p className="notice mt-4" data-testid="native-window-warning">
      {nativeWindowWarning}
    </p>
  );
}

export function PageHeader({
  eyebrow = "RESEARCH WORKSPACE",
  title,
  subtitle,
  children,
}: {
  eyebrow?: string;
  title: string;
  subtitle: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p className="page-subtitle">{subtitle}</p>
      </div>
      {children}
    </header>
  );
}
export function Panel({
  title,
  action,
  children,
  body = true,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  body?: boolean;
}) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
        {action}
      </div>
      <div className={body ? "panel-body" : undefined}>{children}</div>
    </section>
  );
}
export function Loading({
  text = "Loading your workspace…",
}: {
  text?: string;
}) {
  return (
    <div className="loading" role="status">
      <div className="flex flex-col items-center gap-3">
        <LoaderCircle size={24} />
        <span>{text}</span>
      </div>
    </div>
  );
}
export function ErrorState({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  const details = describeError(error);
  return (
    <div className="notice error" role="alert">
      <AlertCircle />
      <div className="min-w-0 flex-1">
        <p>{details.message}</p>
        {details.correlationId && (
          <p className="error-detail">Reference: {details.correlationId}</p>
        )}
        {retry && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-3"
            onClick={retry}
          >
            Try again
          </Button>
        )}
      </div>
    </div>
  );
}
export function DemoNotice() {
  return (
    <div className="notice">
      <FlaskConical />
      <p>
        Fixed-seed research only. Synthetic and provider-fixture datasets are
        labeled separately from live downloads. Native strategy results are not
        independently trusted. No model calls, optimization, live trading, or
        investment recommendation.
      </p>
    </div>
  );
}
export function Status({ status }: { status: string }) {
  return (
    <span className={`status status-${status}`} data-testid="run-status">
      {label(status)}
    </span>
  );
}
