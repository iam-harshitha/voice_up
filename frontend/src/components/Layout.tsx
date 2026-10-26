import { NavLink, Outlet } from "react-router-dom";
import { useAuth, useSession } from "../auth/AuthContext";
import { Button, cx } from "./ui";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/requests", label: "Approval requests", end: false },
  { to: "/requests/new", label: "New request", end: true },
  { to: "/policies", label: "Policies", end: false },
  { to: "/approvers", label: "Approvers", end: false },
];

export function VoiceModeBadge({ provider }: { provider: string }) {
  if (provider === "mock") {
    return (
      <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900 ring-1 ring-amber-300">
        SIMULATED VOICE (mock provider — no real calls)
      </span>
    );
  }
  if (provider === "bolna") {
    return (
      <span className="rounded bg-sky-100 px-2 py-0.5 text-xs font-semibold text-sky-900 ring-1 ring-sky-300">
        Voice: Bolna (real calls)
      </span>
    );
  }
  return (
    <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700 ring-1 ring-slate-300">
      Voice calls disabled
    </span>
  );
}

export function Layout() {
  const session = useSession();
  const { logout } = useAuth();
  return (
    <div className="min-h-screen md:flex">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="border-b border-slate-200 bg-white md:min-h-screen md:w-60 md:border-b-0 md:border-r">
        <div className="px-5 py-4">
          <p className="text-base font-semibold tracking-tight text-slate-900">Approval Line</p>
          <p className="text-xs text-slate-500">Human authorization for AI agents</p>
        </div>
        <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:overflow-visible">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cx(
                  "whitespace-nowrap rounded-md px-3 py-2 text-sm font-medium",
                  isActive ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex-1">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-6 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm font-medium text-slate-900">{session.organization.name}</span>
            <VoiceModeBadge provider={session.environment.voice_provider} />
            {session.environment.app_env !== "production" && (
              <span className="text-xs text-slate-500">env: {session.environment.app_env}</span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-slate-600">{session.user.display_name}</span>
            <Button variant="ghost" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </header>
        <main id="main" className="mx-auto max-w-7xl px-4 py-6 sm:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
