/**
 * Reading the reset token out of the address bar, and taking it straight back out.
 *
 * The link in the message is `https://…/?reset=<token>`, a query parameter on the
 * root rather than a path segment: this application has no router and is served
 * as a single document, so a path would need a rewrite rule in whatever serves it
 * — and a rule that is missing turns every reset link into a 404 the operator
 * hears about from a user.
 *
 * **The token is removed from the URL on the first read**, with
 * `history.replaceState`, before the screen has rendered anything. A bearer
 * credential in an address bar otherwise sits in the browser's history, in the
 * session restore of the next launch, and — on any page that later loads a
 * cross-origin resource — in a `Referer`. None of that is the control that makes
 * the token safe (one use, one hour, a digest at rest, every session revoked on
 * completion); it is the cheap one that removes a whole class of accidents.
 */

/** The parameter name, matching `RESET_TOKEN_QUERY_PARAM` in `services/account_email.py`. */
const PARAM = 'reset';

/** 32 bytes as hex, which is what `secrets.token_hex(32)` produces. */
const SHAPE = /^[0-9a-f]{64}$/;

/**
 * The token this page was opened with, or `null`, having stripped it from the URL.
 *
 * Shape-checked before it is accepted, so a random `?reset=hello` in the address
 * bar shows the sign-in screen rather than a reset form that can only ever fail.
 * The check is not a security control — the server decides — it is what keeps the
 * interface from presenting an impossible task.
 *
 * Safe to call when there is no `window` (it simply answers `null`), and safe to
 * call twice: the second call finds nothing, because the first removed it.
 */
export function takeResetToken(): string | null {
  if (typeof window === 'undefined') return null;

  const url = new URL(window.location.href);
  const candidate = url.searchParams.get(PARAM);
  if (candidate === null) return null;

  url.searchParams.delete(PARAM);
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);

  return SHAPE.test(candidate) ? candidate : null;
}
