import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { getProviderCapabilities } from '../api/endpoints';
import { describeError } from '../api/client';
import type { ProviderCapabilities } from '../api/types';
import { CapabilitiesContext, type CapabilitiesState } from './capabilitiesContext';

interface Result {
  nonce: number;
  capabilities: ProviderCapabilities | null;
  error: string | null;
}

/**
 * Fetches provider capabilities once at startup.
 *
 * The contract is explicit that degraded mode is shown permanently rather than
 * at the moment of failure: the user must know the limit before trying, not
 * after. So this runs on boot, not lazily from the recipes screen.
 */
export function CapabilitiesProvider({ children }: { children: ReactNode }) {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const refresh = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getProviderCapabilities(controller.signal)
      .then((capabilities) => {
        setResult({ nonce, capabilities, error: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ nonce, capabilities: null, error: describeError(cause) });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  const value = useMemo<CapabilitiesState>(
    () => ({
      capabilities: result?.capabilities ?? null,
      loading: result?.nonce !== nonce,
      error: result?.error ?? null,
      refresh,
    }),
    [result, nonce, refresh],
  );

  return <CapabilitiesContext value={value}>{children}</CapabilitiesContext>;
}
