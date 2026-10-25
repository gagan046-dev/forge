import { Link, useLocation } from "react-router-dom";
import { EmptyState, PageHeader } from "../components/ui";

export function NotFoundPage() {
  const { pathname } = useLocation();
  return (
    <div className="page">
      <PageHeader title="Not found" />
      <EmptyState title={`No page at ${pathname}`}>
        Go to <Link to="/chat">Chat</Link> or pick a page from the sidebar.
      </EmptyState>
    </div>
  );
}
