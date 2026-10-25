import { useCallback, useState } from "react";

/**
 * useState that remembers a small UI preference (selected model, preset,
 * usage range) in localStorage. Storage can be unavailable (private windows,
 * blocked site data), so every access is guarded and falls back to memory.
 */
export function useStoredState<T extends string>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      return (window.localStorage.getItem(key) as T | null) ?? initial;
    } catch {
      return initial;
    }
  });

  const update = useCallback(
    (next: T) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, next);
      } catch {
        // Preference just won't persist; nothing else depends on it.
      }
    },
    [key],
  );

  return [value, update];
}
