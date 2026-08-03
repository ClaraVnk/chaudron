import { apiConfig } from './config';
import type { ProblemDetails } from './types';

/**
 * An API failure carrying the RFC 9457 problem document when the server sent
 * one. Callers branch on `problemType` / `status`, never on message text.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetails | null;
  readonly retryAfterSeconds: number | null;

  constructor(
    message: string,
    status: number,
    problem: ProblemDetails | null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** Last path segment of the problem type URI, e.g. `product-not-found`. */
  get problemType(): string | null {
    const type = this.problem?.type;
    if (!type || type === 'about:blank') return null;
    const slug = type.split('/').filter(Boolean).pop();
    return slug ?? null;
  }
}

/** Network unreachable, DNS failure, CORS rejection, offline. */
export class NetworkError extends Error {
  constructor(cause?: unknown) {
    super("Le serveur Chaudron est injoignable. Vérifiez votre connexion ou l'URL de l'API.");
    this.name = 'NetworkError';
    this.cause = cause;
  }
}

export class ConfigurationError extends Error {
  constructor() {
    super("L'application n'est pas configurée.");
    this.name = 'ConfigurationError';
  }
}

function isProblem(value: unknown): value is ProblemDetails {
  return (
    typeof value === 'object' &&
    value !== null &&
    'title' in value &&
    typeof value.title === 'string'
  );
}

function parseRetryAfter(header: string | null): number | null {
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds)) return Math.max(0, Math.round(seconds));
  const date = Date.parse(header);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.round((date - Date.now()) / 1000));
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  query?: Record<string, string | number | undefined>;
  body?: unknown;
  signal?: AbortSignal;
}

/**
 * Single entry point for every call. Sets `X-Household-Id` (the provisional
 * slice-1 household resolution described in the contract) and turns
 * `application/problem+json` into a typed `ApiError`.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (!apiConfig) throw new ConfigurationError();

  const url = new URL(`${apiConfig.baseUrl}/v1${path}`);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
  }

  const headers: Record<string, string> = {
    Accept: 'application/json, application/problem+json',
    'X-Household-Id': apiConfig.householdId,
  };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';

  let response: Response;
  try {
    response = await fetch(url, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal ?? null,
      credentials: 'omit',
      mode: 'cors',
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new NetworkError(cause);
  }

  if (response.status === 204) return undefined as T;

  const raw = await response.text();
  let payload: unknown = null;
  if (raw.length > 0) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const problem = isProblem(payload) ? payload : null;
    const message = problem?.detail ?? problem?.title ?? `Erreur ${String(response.status)}.`;
    throw new ApiError(
      message,
      response.status,
      problem,
      parseRetryAfter(response.headers.get('Retry-After')),
    );
  }

  return payload as T;
}

/** Human-readable text for any error surfaced to the user. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return 'Foyer inconnu ou en-tête X-Household-Id absent. Vérifiez VITE_HOUSEHOLD_ID dans votre configuration.';
    }
    if (error.status >= 500) {
      return 'Le serveur Chaudron a rencontré une erreur. Réessayez dans un instant.';
    }
    return error.message;
  }
  if (error instanceof NetworkError || error instanceof ConfigurationError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Une erreur inattendue est survenue.';
}
