import { Component, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { LoadingBlock } from "./components/ui";
import { ApproversPage } from "./pages/ApproversPage";
import { LoginPage } from "./pages/LoginPage";
import { NewRequestPage } from "./pages/NewRequestPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { RequestDetailPage } from "./pages/RequestDetailPage";
import { RequestsPage } from "./pages/RequestsPage";

class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  override render() {
    if (this.state.failed) {
      return (
        <div role="alert" className="mx-auto mt-16 max-w-lg rounded-lg border border-red-200 bg-white p-6">
          <h1 className="text-lg font-semibold text-red-900">Something went wrong in the dashboard.</h1>
          <p className="mt-2 text-sm text-slate-700">
            No action was taken. Reload the page to continue; approval state is kept on the server.
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

export function App() {
  const { state } = useAuth();
  if (state.status === "loading") return <LoadingBlock label="Checking your session" />;
  if (state.status === "signed_out") return <LoginPage notice={state.reason} />;
  return (
    <ErrorBoundary>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<OverviewPage />} />
          <Route path="requests" element={<RequestsPage />} />
          <Route path="requests/new" element={<NewRequestPage />} />
          <Route path="requests/:requestId" element={<RequestDetailPage />} />
          <Route path="policies" element={<PoliciesPage />} />
          <Route path="approvers" element={<ApproversPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </ErrorBoundary>
  );
}
