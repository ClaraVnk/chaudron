import { createContext, useContext } from 'react';
import type { ProviderCapabilities } from '../api/types';

export interface CapabilitiesState {
  /** null while loading, or when the call failed. */
  capabilities: ProviderCapabilities | null;
  loading: boolean;
  /** Set when /v1/providers/capabilities itself could not be reached. */
  error: string | null;
  refresh: () => void;
}

export const CapabilitiesContext = createContext<CapabilitiesState | null>(null);

export function useCapabilities(): CapabilitiesState {
  const value = useContext(CapabilitiesContext);
  if (!value) throw new Error('useCapabilities must be used inside <CapabilitiesProvider>.');
  return value;
}
