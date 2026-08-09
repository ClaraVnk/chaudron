import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { useCapabilities } from '../../context/capabilitiesContext';
import {
  archiveProviderConfig,
  createProviderConfig,
  getProviderCatalogue,
  getProviderConfigs,
  grantProviderConsent,
  probeProviderConfig,
  updateProviderConfig,
  withdrawProviderConsent,
  type ProviderChoice,
  type ProviderConfig,
  type ProviderConfigDraft,
  type ProviderConfigPatch,
} from './api';

/**
 * The household's provider configurations, and the catalogue the form is built from.
 *
 * Both are fetched together and reloaded together, because the screen is useless
 * with one of them: a list with no catalogue cannot offer a model, and a catalogue
 * with no list cannot say what is already configured.
 *
 * **Every mutation refreshes the capability banner too.** That banner is what the
 * rest of the application reads to decide whether recipe suggestions exist at all
 * (`CapabilitiesProvider`), and it is computed server-side from the configuration
 * this screen has just changed. Without the refresh, a household that has just
 * entered its key would still be told the feature is unavailable until it reloaded
 * the page — which is exactly the confusion this screen exists to end.
 *
 * Nothing here holds a key: the value typed into the form lives in the form's own
 * state for the length of one submission, and no response carries it back.
 */

interface Loaded {
  nonce: number;
  configs: ProviderConfig[];
  catalogue: ProviderChoice[];
  error: string | null;
  /** The server predates these routes: an older deployment, not a broken one. */
  unsupported: boolean;
}

export interface ProviderConfigsState {
  configs: ProviderConfig[];
  catalogue: ProviderChoice[];
  loading: boolean;
  error: string | null;
  unsupported: boolean;
  reload: () => void;
  create: (draft: ProviderConfigDraft) => Promise<void>;
  update: (id: string, patch: ProviderConfigPatch) => Promise<void>;
  archive: (id: string) => Promise<void>;
  grantConsent: (id: string) => Promise<void>;
  withdrawConsent: (id: string) => Promise<void>;
  probe: (id: string) => Promise<void>;
}

export function useProviderConfigs(): ProviderConfigsState {
  const [nonce, setNonce] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const { refresh: refreshCapabilities } = useCapabilities();

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    Promise.all([getProviderConfigs(controller.signal), getProviderCatalogue(controller.signal)])
      .then(([configs, catalogue]) => {
        setLoaded({ nonce, configs, catalogue, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          nonce,
          configs: [],
          catalogue: [],
          // A 404 here is a deployment that predates the routes, not a household
          // with nothing configured — that state is a `200` and an empty array.
          error: cause instanceof ApiError && cause.status === 404 ? null : describeError(cause),
          unsupported: cause instanceof ApiError && (cause.status === 404 || cause.status === 501),
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  const after = useCallback(
    async (work: Promise<unknown>) => {
      await work;
      reload();
      refreshCapabilities();
    },
    [refreshCapabilities, reload],
  );

  return {
    configs: loaded?.configs ?? [],
    catalogue: loaded?.catalogue ?? [],
    loading: loaded?.nonce !== nonce,
    error: loaded?.error ?? null,
    unsupported: loaded?.unsupported ?? false,
    reload,
    create: useCallback(
      async (draft: ProviderConfigDraft) => {
        await after(createProviderConfig(draft));
      },
      [after],
    ),
    update: useCallback(
      async (id: string, patch: ProviderConfigPatch) => {
        await after(updateProviderConfig(id, patch));
      },
      [after],
    ),
    archive: useCallback(
      async (id: string) => {
        await after(archiveProviderConfig(id));
      },
      [after],
    ),
    grantConsent: useCallback(
      async (id: string) => {
        await after(grantProviderConsent(id));
      },
      [after],
    ),
    withdrawConsent: useCallback(
      async (id: string) => {
        await after(withdrawProviderConsent(id));
      },
      [after],
    ),
    probe: useCallback(
      async (id: string) => {
        await after(probeProviderConfig(id));
      },
      [after],
    ),
  };
}
