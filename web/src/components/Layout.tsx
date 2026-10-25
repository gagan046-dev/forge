import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { describeError } from "../api/client";
import { getStatus } from "../api/endpoints";
import { useAuth } from "../auth";
import { usePolling } from "../lib/usePolling";
import { BackendState } from "./BackendState";

const NAV: { group: string; items: { to: string; label: string }[] }[] = [
  {
    group: "Workspace",
    items: [
      { to: "/chat", label: "Chat" },
      { to: "/repos", label: "Ask repo" },
    ],
  },
  {
    group: "Admin",
    items: [
      { to: "/keys", label: "API keys" },
      { to: "/usage", label: "Usage" },
      { to: "/system", label: "System" },
    ],
  },
  {
    group: "Setup",
    items: [{ to: "/connect", label: "Connect" }],
  },
];

/** dev / prod from the ingress host, so it is obvious which environment you are changing. */
function environmentLabel(): string {
  const host = window.location.hostname;
  if (host.startsWith("forge-dev.")) return "dev";
  if (host.startsWith("forge.")) return "prod";
  return "local";
}

export function Layout() {
  const { signOut } = useAuth();
  const status = usePolling((signal) => getStatus(signal), 10_000);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const env = environmentLabel();

  const onSignOut = async () => {
    setSignOutError(null);
    try {
      await signOut();
    } catch (err) {
      setSignOutError(describeError(err));
    }
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <span className="brand-name">forge</span>
          <span className={`env env-${env}`}>{env}</span>
        </div>

        <nav className="nav" aria-label="Main">
          {NAV.map((section) => (
            <div key={section.group} className="nav-group">
              <div className="nav-heading">{section.group}</div>
              {section.items.map((item) => (
                <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>

        <div className="sidebar-foot">
          <NavLink to="/system" className="foot-status" title="Inference backend">
            {status.data ? (
              <>
                <BackendState state={status.data.backend} />
                <span className="mono muted">
                  {status.data.inflight} run · {status.data.queue.autocomplete + status.data.queue.chat + status.data.queue.batch} queued
                </span>
              </>
            ) : status.error ? (
              <span className="muted">gateway unreachable</span>
            ) : (
              <span className="muted">checking backend…</span>
            )}
          </NavLink>
          <button type="button" className="link-button" onClick={() => void onSignOut()}>
            Sign out
          </button>
          {signOutError && <div className="foot-error">{signOutError}</div>}
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
