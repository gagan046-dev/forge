import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { KeysPage } from "./pages/Keys";
import { LoginPage } from "./pages/Login";
import { NotFoundPage } from "./pages/NotFound";
import { SystemPage } from "./pages/System";

// Markdown + highlight.js and uPlot are most of the bundle; load them with the pages that use them.
const ChatPage = lazy(() => import("./pages/Chat").then((m) => ({ default: m.ChatPage })));
const AskRepoPage = lazy(() => import("./pages/AskRepo").then((m) => ({ default: m.AskRepoPage })));
const WorkspacePage = lazy(() => import("./pages/Workspace").then((m) => ({ default: m.WorkspacePage })));
const ConnectPage = lazy(() => import("./pages/Connect").then((m) => ({ default: m.ConnectPage })));
const UsagePage = lazy(() => import("./pages/Usage").then((m) => ({ default: m.UsagePage })));

const lazyPage = (page: ReactNode) => <Suspense fallback={<div className="boot">Loading…</div>}>{page}</Suspense>;

function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const location = useLocation();
  if (state === "checking") return <div className="boot">Checking session…</div>;
  if (state === "signed-out") {
    const next = location.pathname + location.search;
    return <Navigate to={`/login${next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`} replace />;
  }
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/chat" replace />} />
        <Route path="chat" element={lazyPage(<ChatPage />)} />
        <Route path="chat/:conversationId" element={lazyPage(<ChatPage />)} />
        <Route path="repos" element={lazyPage(<AskRepoPage />)} />
        <Route path="workspace" element={lazyPage(<WorkspacePage />)} />
        <Route path="workspace/:name" element={lazyPage(<WorkspacePage />)} />
        <Route path="keys" element={<KeysPage />} />
        <Route path="usage" element={lazyPage(<UsagePage />)} />
        <Route path="system" element={<SystemPage />} />
        <Route path="connect" element={lazyPage(<ConnectPage />)} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
