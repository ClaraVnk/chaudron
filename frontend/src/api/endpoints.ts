import { request } from './client';
import type {
  CreateInventoryItem,
  InventoryItem,
  InventoryPage,
  InventoryQuery,
  Product,
  ProviderCapabilities,
  RemovalReason,
  StorageLocation,
  SuggestRecipesRequest,
  SuggestRecipesResponse,
} from './types';

export function getLocations(signal?: AbortSignal): Promise<StorageLocation[]> {
  return request<StorageLocation[]>('/locations', { signal });
}

export function getInventory(query: InventoryQuery, signal?: AbortSignal): Promise<InventoryPage> {
  return request<InventoryPage>('/inventory', {
    query: {
      location_id: query.location_id,
      q: query.q,
      expiring_within_days: query.expiring_within_days,
      limit: query.limit,
      offset: query.offset,
    },
    signal,
  });
}

export function createInventoryItem(
  body: CreateInventoryItem,
  signal?: AbortSignal,
): Promise<InventoryItem> {
  return request<InventoryItem>('/inventory', { method: 'POST', body, signal });
}

// `PATCH /v1/inventory/{id}` is in the contract but has no caller in this
// slice: nothing edits an existing item yet. Add the wrapper with the screen
// that needs it rather than leaving an untested function behind.

export function deleteInventoryItem(
  id: string,
  reason: RemovalReason = 'consumed',
  signal?: AbortSignal,
): Promise<void> {
  return request<void>(`/inventory/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    query: { reason },
    signal,
  });
}

/**
 * Resolves a GTIN. Callers must screen out store-internal codes first (see
 * `isRestrictedCirculationNumber`): those are guaranteed 404s that burn the
 * shared Open Food Facts rate limit. The server's 422 is a safety net, not the
 * intended path.
 */
export function lookupProduct(gtin: string, signal?: AbortSignal): Promise<Product> {
  return request<Product>('/products/lookup', { query: { gtin }, signal });
}

export function suggestRecipes(
  body: SuggestRecipesRequest,
  signal?: AbortSignal,
): Promise<SuggestRecipesResponse> {
  return request<SuggestRecipesResponse>('/recipes/suggest', { method: 'POST', body, signal });
}

export function getProviderCapabilities(signal?: AbortSignal): Promise<ProviderCapabilities> {
  return request<ProviderCapabilities>('/providers/capabilities', { signal });
}
