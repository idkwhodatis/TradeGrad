import { useRef, useState } from "react";

// React disabled state alone doesn't guard two clicks before the next render.
export function useDataAction<T extends unknown[]>(
  perform: (...args: T) => Promise<void>,
) {
  const guard = useRef(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function run(...args: T) {
    if (guard.current) return;
    guard.current = true;
    setPending(true);
    setError(null);
    try {
      await perform(...args);
    } catch (failure) {
      setError(failure);
    } finally {
      guard.current = false;
      setPending(false);
    }
  }
  return { run, pending, error, resetError: () => setError(null) };
}
