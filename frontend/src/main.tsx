import React, { useEffect } from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  lazyRouteComponent,
  redirect,
  RouterProvider,
} from "@tanstack/react-router";
import { AppLayout } from "@/components/layout";
import { ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { queryClient } from "@/lib/api";
import {
  clearSession,
  readSession,
  safeReturnTo,
  watchSession,
} from "@/lib/session";
import "./styles.css";

function Root() {
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    function sync(event?: Event) {
      clearTimeout(timer);
      const session = readSession();
      if (session)
        timer = setTimeout(
          () => clearSession(true),
          Math.max(0, session.expiresAt - Date.now()),
        );
      else if (router.state.location.pathname !== "/login") {
        queryClient.clear();
        void router.navigate({
          to: "/login",
          search: {
            next: safeReturnTo(router.state.location.href),
            expired: Boolean((event as CustomEvent)?.detail?.expired),
          },
          replace: true,
        });
      }
    }
    sync();
    const unsubscribe = watchSession(sync);
    return () => {
      clearTimeout(timer);
      unsubscribe();
    };
  }, []);
  return <Outlet />;
}
const rootRoute = createRootRoute({
  component: Root,
  errorComponent: ({ error, reset }) => (
    <div className="page">
      <h1>Something interrupted this page</h1>
      <ErrorState error={error} retry={reset} />
      <Button asChild variant="outline" className="mt-5">
        <Link to="/">Return to overview</Link>
      </Button>
    </div>
  ),
  notFoundComponent: () => (
    <div className="page">
      <h1>Page not found</h1>
      <p className="muted">This address does not point to a workspace page.</p>
      <Button asChild className="mt-5">
        <Link to="/">Return to overview</Link>
      </Button>
    </div>
  ),
});
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: lazyRouteComponent(() => import("@/features/login"), "LoginPage"),
  validateSearch: (search: Record<string, unknown>) => ({
    next: safeReturnTo(search.next),
    expired: search.expired === true || search.expired === "true",
  }),
});
const privateRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "workspace",
  component: AppLayout,
  beforeLoad: ({ location }) => {
    if (!readSession())
      throw redirect({
        to: "/login",
        search: { next: safeReturnTo(location.href), expired: false },
        replace: true,
      });
  },
});
const overviewRoute = createRoute({
  getParentRoute: () => privateRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/features/overview"),
    "OverviewPage",
  ),
});
const datasetsRoute = createRoute({
  getParentRoute: () => privateRoute,
  path: "/datasets",
  component: lazyRouteComponent(
    () => import("@/features/catalogs"),
    "DatasetsPage",
  ),
});
const strategiesRoute = createRoute({
  getParentRoute: () => privateRoute,
  path: "/strategies",
  component: lazyRouteComponent(
    () => import("@/features/catalogs"),
    "StrategiesPage",
  ),
});
const runsRoute = createRoute({
  getParentRoute: () => privateRoute,
  path: "/runs",
  component: lazyRouteComponent(
    () => import("@/features/overview"),
    "RunsPage",
  ),
});
const newRunRoute = createRoute({
  getParentRoute: () => privateRoute,
  path: "/runs/new",
  component: lazyRouteComponent(
    () => import("@/features/new-run"),
    "NewRunPage",
  ),
});
const runRoute = createRoute({
  getParentRoute: () => privateRoute,
  path: "/runs/$runId",
  component: lazyRouteComponent(
    () => import("@/features/run-detail"),
    "RunDetailPage",
  ),
});
const routeTree = rootRoute.addChildren([
  loginRoute,
  privateRoute.addChildren([
    overviewRoute,
    datasetsRoute,
    strategiesRoute,
    runsRoute,
    newRunRoute,
    runRoute,
  ]),
]);
const router = createRouter({
  routeTree,
  defaultPreload: "intent",
  scrollRestoration: true,
});
declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
