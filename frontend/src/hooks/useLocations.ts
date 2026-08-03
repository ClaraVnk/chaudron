import { useCallback, useEffect, useState } from 'react';
import { getLocations } from '../api/endpoints';
import { describeError } from '../api/client';
import type { StorageLocation } from '../api/types';

interface Result {
  nonce: number;
  locations: StorageLocation[];
  error: string | null;
}

/** Stable identity: consumers put this array in memo dependencies. */
const NO_LOCATIONS: StorageLocation[] = [];

export interface LocationsState {
  locations: StorageLocation[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * Storage locations are read by both the inventory screen and the add form.
 * Lifting them to the shell keeps one request instead of two and keeps the two
 * screens showing the same list.
 *
 * `loading` is derived from "the answer I hold is not the answer I asked for"
 * rather than set synchronously inside the effect. That avoids a cascading
 * render, and it keeps the previous list on screen during a refresh instead of
 * blanking it.
 */
export function useLocations(): LocationsState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    getLocations(controller.signal)
      .then((locations) => {
        setResult({ nonce, locations, error: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult((previous) => ({
          nonce,
          locations: previous?.locations ?? NO_LOCATIONS,
          error: describeError(cause),
        }));
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  return {
    locations: result?.locations ?? NO_LOCATIONS,
    loading: result?.nonce !== nonce,
    error: result?.error ?? null,
    reload,
  };
}
