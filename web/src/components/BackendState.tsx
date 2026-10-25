import type { BackendState as State } from "../api/types";

const LABELS: Record<State, string> = {
  ready: "ready",
  waking: "waking",
  sleeping: "sleeping",
};

/** Status dot plus text label, so state is never conveyed by colour alone. */
export function BackendState({ state }: { state: State }) {
  return (
    <span className={`state state-${state}`}>
      <span className="dot" aria-hidden="true" />
      {LABELS[state]}
    </span>
  );
}
