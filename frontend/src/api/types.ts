/**
 * Wire types for the Chaudron API v1.
 *
 * Mirrors docs/api-contract-v1.md exactly. The contract is written before both
 * sides of the code; anything that drifts from it here breaks integration, so
 * changes belong in the document first.
 */

export type LocationKind = 'fridge' | 'freezer' | 'pantry' | 'cellar' | 'other';

export interface StorageLocation {
  id: string;
  name: string;
  kind: LocationKind;
  item_count: number;
}

/** Locations are embedded in inventory items without their item count. */
export type EmbeddedLocation = Pick<StorageLocation, 'id' | 'name' | 'kind'>;

export interface Product {
  id: string;
  name: string;
  brand: string | null;
  gtin: string | null;
  image_url: string | null;
}

export interface Quantity {
  /**
   * A decimal string, never a number. JSON floats destroy exact decimals, and a
   * quantity wrong by a factor of ten in a food inventory is not a detail.
   */
  amount: string;
  unit: string;
}

export type ExpiryKind = 'use_by' | 'best_before';
export type ItemSource = 'manual' | 'barcode_scan' | 'receipt_import';
export type RemovalReason = 'consumed' | 'wasted' | 'correction';

export interface InventoryItem {
  id: string;
  product: Product;
  location: EmbeddedLocation;
  quantity: Quantity;
  expires_on: string | null;
  expiry_kind: ExpiryKind | null;
  opened_at: string | null;
  source: ItemSource;
  created_at: string;
}

export interface InventoryPage {
  total: number;
  items: InventoryItem[];
}

export interface InventoryQuery {
  location_id?: string;
  q?: string;
  expiring_within_days?: number;
  limit?: number;
  offset?: number;
}

/** Manual product creation, inline in a POST /v1/inventory body. */
export interface ProductDraft {
  name: string;
  brand: string | null;
  gtin: string | null;
  default_unit: string;
}

/**
 * `product_id` OR `product` must be supplied, never both — the union keeps that
 * rule in the type system rather than in a comment nobody reads.
 */
export type CreateInventoryItem = {
  location_id: string;
  amount: string;
  unit: string;
  expires_on: string | null;
  expiry_kind: ExpiryKind | null;
  source: ItemSource;
} & ({ product_id: string; product?: never } | { product: ProductDraft; product_id?: never });

export interface RecipeIngredient {
  name: string;
  amount: string | null;
  unit: string | null;
  in_stock: boolean;
}

export interface RecipeSuggestion {
  id: string;
  title: string;
  summary: string;
  duration_minutes: number | null;
  servings: number | null;
  ingredients: RecipeIngredient[];
  steps: string[];
  uses_expiring_soon: boolean;
}

export interface SuggestRecipesRequest {
  location_ids: string[];
  max_suggestions: number;
  notes: string;
}

export interface SuggestRecipesResponse {
  provider_mode: string;
  model: string;
  suggestions: RecipeSuggestion[];
}

/**
 * The contract states degraded_reasons "lists in plain language what is reduced
 * and why" and shows an empty array. Accepting an object shape as well costs
 * nothing and keeps the banner from crashing if the backend sends structured
 * reasons; `formatDegradedReason` normalises both.
 */
export type DegradedReason =
  string | { code?: string; title?: string; detail?: string; message?: string; reason?: string };

export interface ProviderCapabilities {
  configured: boolean;
  mode: string | null;
  provider: string | null;
  model: string | null;
  capabilities: {
    vision: boolean;
    structured_output: boolean;
  };
  degraded: boolean;
  degraded_reasons: DegradedReason[];
}

/** RFC 9457 problem details. */
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  [extension: string]: unknown;
}

export function formatDegradedReason(reason: DegradedReason): string {
  if (typeof reason === 'string') return reason;
  return (
    reason.detail ??
    reason.message ??
    reason.title ??
    reason.reason ??
    reason.code ??
    'Raison non précisée.'
  );
}
