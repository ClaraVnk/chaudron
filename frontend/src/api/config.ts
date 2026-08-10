/**
 * Where the API lives, resolved once at module load.
 *
 * **The default is the origin the page was served from, and that is what makes
 * one build deployable everywhere.** `VITE_API_BASE_URL` used to be required,
 * which meant the JavaScript chunk carried a hard-coded hostname: a bundle built
 * for one instance was worthless to any other, so the artefact could not be
 * published — every operator had to build their own, and every deployment
 * carried a tarball nobody could verify against a signature. Reading
 * `window.location.origin` instead costs nothing and removes that entirely.
 *
 * It is also correct rather than merely convenient. The session cookie is
 * `__Host-` prefixed and `SameSite=Lax`, so the API has to be same-site with the
 * page or the cookie is never sent; the documented deployment puts both behind
 * one reverse proxy on one origin. Deriving the base URL from the page is
 * therefore not a guess — it is the arrangement the cookie already requires.
 *
 * `VITE_API_BASE_URL` survives as an **override**, for the one supported layout
 * the default cannot express: `api.example.org` beside `app.example.org`, same
 * registrable domain, different origin. Setting it re-introduces the hard-coded
 * hostname and makes the bundle instance-specific again — which is a fair trade
 * when you need it, and a trap when you set it out of habit.
 *
 * **There is exactly one value here, and it is a URL.** `VITE_HOUSEHOLD_ID` used
 * to live alongside it and had to go, because a `VITE_*` variable is not
 * configuration in any private sense: Vite substitutes it at build time and
 * inlines the literal into `dist/assets/index-*.js`, which the service worker
 * then precaches onto every visitor's device. While `X-Household-Id` was the
 * whole of the access control, that meant the credential was *shipped with the
 * application* — loading the page was enough to read it, and CORS changed
 * nothing, being a browser-side rule that `curl` has never heard of.
 *
 * So the active household is **runtime state derived from the session**
 * (`api/session.ts`), not a build constant. That is also what makes a household
 * *selector* possible at all: one build used to mean one household, forever.
 *
 * The rule this file has to keep: nothing secret, and nothing
 * authorisation-bearing, may ever be read from `import.meta.env` again.
 */

export interface ApiConfig {
  baseUrl: string;
}

function read(name: string): string {
  const raw = (import.meta.env[name] as string | undefined) ?? '';
  return raw.trim();
}

/**
 * The page's own origin, or `null` where there is no page.
 *
 * Guarded because this module is imported by unit tests running under Node,
 * where `window` does not exist. Throwing there would fail the suite on an
 * environment detail rather than on anything about the application.
 */
function pageOrigin(): string | null {
  if (typeof window === 'undefined') return null;
  const origin = window.location?.origin ?? '';
  // `origin` is the string "null" for an opaque origin — a `file://` page, or a
  // sandboxed frame. Treating that as a base URL produces requests to the
  // literal host "null", which fail late and confusingly.
  return origin && origin !== 'null' ? origin : null;
}

function build(): { config: ApiConfig | null; error: string | null } {
  const override = read('VITE_API_BASE_URL').replace(/\/+$/, '');
  if (override) {
    return { config: { baseUrl: override }, error: null };
  }

  const origin = pageOrigin();
  if (origin) {
    return { config: { baseUrl: origin }, error: null };
  }

  return {
    config: null,
    error:
      "Impossible de déterminer l'adresse de l'API : cette page n'a pas d'origine " +
      'exploitable. Servez-la en http(s), ou renseignez VITE_API_BASE_URL à la construction.',
  };
}

const result = build();

export const apiConfig: ApiConfig | null = result.config;
export const configError: string | null = result.error;
