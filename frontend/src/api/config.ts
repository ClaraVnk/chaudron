/**
 * Build-time configuration, validated once at module load.
 *
 * Fail fast and visibly: a PWA pointed at nothing produces a stream of network
 * errors that look like backend outages. `configError` lets the shell render one
 * honest screen instead.
 */

export interface ApiConfig {
  baseUrl: string;
  householdId: string;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function read(name: string): string {
  const raw = (import.meta.env[name] as string | undefined) ?? '';
  return raw.trim();
}

function build(): { config: ApiConfig | null; error: string | null } {
  const baseUrl = read('VITE_API_BASE_URL').replace(/\/+$/, '');
  const householdId = read('VITE_HOUSEHOLD_ID');
  const missing: string[] = [];

  if (!baseUrl) missing.push('VITE_API_BASE_URL');
  if (!householdId) missing.push('VITE_HOUSEHOLD_ID');
  if (missing.length > 0) {
    return {
      config: null,
      error: `Variables d'environnement manquantes : ${missing.join(', ')}. Copiez .env.example vers .env.local, renseignez-les, puis relancez la construction.`,
    };
  }
  if (!UUID_RE.test(householdId)) {
    return {
      config: null,
      error: `VITE_HOUSEHOLD_ID n'est pas un UUID valide. L'API répondra 401 sur chaque appel.`,
    };
  }
  return { config: { baseUrl, householdId }, error: null };
}

const result = build();

export const apiConfig: ApiConfig | null = result.config;
export const configError: string | null = result.error;
