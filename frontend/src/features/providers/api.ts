import { request } from '../../api/client';

/**
 * The provider configuration endpoints, and the types they speak.
 *
 * Colocated with the screen rather than added to `api/endpoints.ts` for the reason
 * `api/routers/providers.py` keeps its own request and response models instead of
 * putting them in `api/schemas.py`: one feature uses them, and nothing else has a
 * reason to import them. The shared `request` is still the only way out — the
 * household header, the CSRF token, the problem-document decoding and the
 * session-lost signal all live there and none of them is reimplemented here.
 *
 * **Nothing in this module holds a key.** The value typed into the form lives in
 * the form's own state for the length of one submission. No response type has a
 * field that could carry one back, which mirrors the server: the router's response
 * models have nowhere to put a key either, so a leak would take a change on both
 * sides rather than a forgotten `delete`.
 */

/** Who provides — and pays for — the model access. */
export type ProviderMode = 'byok' | 'ollama' | 'instance_owner';

export type ProviderConfigStatus = 'unverified' | 'verified' | 'invalid_credentials' | 'disabled';

/** One provider this instance offers, as `GET /v1/providers/catalogue` describes it. */
export interface ProviderChoice {
  code: string;
  display_name: string;
  requires_api_key: boolean;
  requires_base_url: boolean;
  default_model: string | null;
  /**
   * Empty for Ollama, where the model is whatever the household pulled and only a
   * probe can say what it does — so the form offers free text there and a closed
   * list everywhere else. The list comes from the server rather than from a
   * constant here, so the two can never drift into offering a model the server
   * would refuse.
   */
  models: string[];
}

export interface ProviderConfig {
  id: string;
  label: string;
  mode: ProviderMode;
  provider: string;
  model: string;
  /** Only ever set for Ollama. Shown back so it can be corrected. */
  base_url: string | null;
  /**
   * The last four characters of the stored key, and the only fragment of it that
   * ever leaves the server. Enough to recognise which of two keys is installed,
   * useless to anybody else.
   */
  api_key_last4: string | null;
  api_key_set_at: string | null;
  status: ProviderConfigStatus;
  capabilities: { vision: boolean; structured_output: boolean };
  max_context_tokens: number | null;
  last_verified_at: string | null;
  last_error: string | null;
  consented_at: string | null;
  consent_revoked_at: string | null;
  /**
   * Whether this configuration needs an agreement at all. `false` only for an
   * Ollama the server would reach without leaving a local network — computed
   * there, because deriving it from `mode` is what let a *hosted* Ollama transmit
   * with the consent gate switched off.
   */
  consent_required: boolean;
  /** The one field to branch on when deciding whether the feature is available. */
  is_permitted: boolean;
  is_consented: boolean;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface ProviderConfigDraft {
  label: string;
  mode: ProviderMode;
  provider: string;
  model: string;
  /**
   * Required, with no default, exactly as the server requires it. A default of
   * `true` would be a pre-ticked box; a default of `false` would let a screen omit
   * the one field the whole feature turns on.
   */
  consent_granted: boolean;
  base_url?: string;
  api_key?: string;
}

export interface ProviderConfigPatch {
  label?: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  disabled?: boolean;
}

export function getProviderCatalogue(signal?: AbortSignal): Promise<ProviderChoice[]> {
  return request<ProviderChoice[]>('/providers/catalogue', { signal });
}

export function getProviderConfigs(signal?: AbortSignal): Promise<ProviderConfig[]> {
  return request<ProviderConfig[]>('/providers', { signal });
}

/**
 * Registers a configuration, with the agreement that lets it be used.
 *
 * The key travels in the body and never in the path or the query string: a
 * credential in a URL lands in access logs, proxy logs and browser history.
 * Nothing in the response carries it back.
 */
export function createProviderConfig(
  body: ProviderConfigDraft,
  signal?: AbortSignal,
): Promise<ProviderConfig> {
  return request<ProviderConfig>('/providers', { method: 'POST', body, signal });
}

/** Sending `api_key` again is the rotation procedure: the old value is overwritten. */
export function updateProviderConfig(
  id: string,
  body: ProviderConfigPatch,
  signal?: AbortSignal,
): Promise<ProviderConfig> {
  return request<ProviderConfig>(`/providers/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body,
    signal,
  });
}

/** Retires it. Not a deletion: the row survives, so this answers with it. */
export function archiveProviderConfig(id: string, signal?: AbortSignal): Promise<ProviderConfig> {
  return request<ProviderConfig>(`/providers/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    signal,
  });
}

export function grantProviderConsent(id: string, signal?: AbortSignal): Promise<ProviderConfig> {
  return request<ProviderConfig>(`/providers/${encodeURIComponent(id)}/consent`, {
    method: 'POST',
    body: { granted: true },
    signal,
  });
}

/**
 * Withdraws the agreement. It takes effect at the next request, and the record of
 * what was authorised — and when it stopped — survives (GDPR art. 7(3)).
 */
export function withdrawProviderConsent(id: string, signal?: AbortSignal): Promise<ProviderConfig> {
  return request<ProviderConfig>(`/providers/${encodeURIComponent(id)}/consent`, {
    method: 'DELETE',
    signal,
  });
}

/** Ollama only: asks the household's own server again what its model can do. */
export function probeProviderConfig(id: string, signal?: AbortSignal): Promise<ProviderConfig> {
  return request<ProviderConfig>(`/providers/${encodeURIComponent(id)}/probe`, {
    method: 'POST',
    signal,
  });
}
