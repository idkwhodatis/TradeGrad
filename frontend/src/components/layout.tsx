import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import {
  ChartNoAxesCombined,
  LayoutDashboard,
  Database,
  Braces,
  FlaskConical,
  LogOut,
  ArrowUpRight,
  CircleHelp,
  Globe,
  Layers,
  Download,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { currentUserOptions, queryClient } from "@/lib/api";
import { clearSession } from "@/lib/session";

const navigation = [
  { to: "/", label: "Overview", icon: LayoutDashboard },
  { to: "/data-sources", label: "Data sources", icon: Globe },
  { to: "/imports", label: "Imports", icon: Download },
  { to: "/universes", label: "Universes", icon: Layers },
  { to: "/datasets", label: "Datasets", icon: Database },
  { to: "/strategies", label: "Strategy library", icon: Braces },
  { to: "/runs", label: "Experiments", icon: FlaskConical },
] as const;

export function AppLayout() {
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  });
  const user = useQuery(currentUserOptions);
  const activeTitle =
    navigation.find((item) =>
      item.to === "/" ? pathname === "/" : pathname.startsWith(item.to),
    )?.label ?? "Experiment";
  function signOut() {
    clearSession();
    queryClient.clear();
  }
  return (
    <div className="app-shell">
      <a className="sr-only focus:not-sr-only" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar">
        <Link to="/" className="brand" aria-label="TradeGrad overview">
          <span className="brand-icon">
            <ChartNoAxesCombined size={23} />
          </span>
          TradeGrad
        </Link>
        <div className="workspace">
          <span className="workspace-avatar">R</span>
          <div>
            <div className="text-xs font-semibold">Research workspace</div>
            <div className="mt-1 text-[10px] text-[#8a967d]">
              Private · Data & research
            </div>
          </div>
        </div>
        <nav aria-label="Main navigation">
          <span className="eyebrow">Workspace</span>
          <div className="nav-list">
            {navigation.map(({ to, label, icon: Icon }) => (
              <Link
                key={to}
                to={to}
                className={`nav-link ${(to === "/" ? pathname === "/" : pathname.startsWith(to)) ? "active" : ""}`}
              >
                <Icon />
                {label}
              </Link>
            ))}
          </div>
        </nav>
        <div className="sidebar-foot">
          <div className="flex items-center gap-2 text-[#60754e]">
            <CircleHelp size={15} />
            <strong className="font-medium">Built for reproducibility</strong>
          </div>
          <p>
            Fixed inputs. Durable events.
            <br />
            Evidence you can revisit.
          </p>
          <a
            href="https://github.com/idkwhodatis/TradeGrad"
            target="_blank"
            rel="noreferrer"
            className="text-link"
          >
            Project repository
            <ArrowUpRight size={13} />
          </a>
        </div>
      </aside>
      <div className="main">
        <div className="topbar">
          <div className="topbar-title">
            Workspace <span className="mx-3 text-[#c0c9b6]">/</span>{" "}
            <strong>{activeTitle}</strong>
          </div>
          <div className="user-chip">
            <span className="chip">Seed evaluation</span>
            <span>{user.data?.email}</span>
            <div className="user-avatar" aria-hidden="true">
              {(user.data?.full_name || user.data?.email || "R")
                .slice(0, 1)
                .toUpperCase()}
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Sign out"
              title="Sign out"
              onClick={signOut}
            >
              <LogOut size={15} />
            </Button>
          </div>
        </div>
        <main className="page" id="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
