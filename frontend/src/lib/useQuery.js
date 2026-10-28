import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';

/** Tiny data hook: runs the fetcher when deps change; ignores stale responses. */
export function useQuery(fetcher, deps, opts = {}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const seq = useRef(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const enabled = opts.enabled ?? true;

  const run = useCallback(async () => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const d = await fetcherRef.current();
      if (id === seq.current) setData(d);
    } catch (e) {
      if (id === seq.current) setError(errorMessage(e));
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [enabled]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    void run();
  }, [run, ...deps]);
  return { data, loading, error, reload: run, setData };
}

/** Wrap an async action with busy state and a toast on failure. */
export function useAction(fn) {
  const [busy, setBusy] = useState(false);
  const wrapped = useCallback(
    async (...args) => {
      setBusy(true);
      try {
        await fn(...args);
      } finally {
        setBusy(false);
      }
    },
    [fn],
  );
  return [wrapped, busy];
}
