/**
 * The six calls that make up a session, and the shape the server answers with.
 *
 * The credential never appears here. Sign-in returns a `Set-Cookie` the browser
 * stores and this code cannot read; what the body carries is the CSRF token and
 * the list of households the account may open.
 *
 * The last two — `revokeAllSessions` and `changePassword` — are what a person who
 * thinks their session has leaked can do about it. Both answer with a **new**
 * `Session`, because both end every session of the account including the one that
 * made the call: a stolen cookie is a copy of *this* one, so sparing it would
 * spare the thief. The caller must adopt the answer (`SessionProvider.adopt`) or
 * its next write will echo a CSRF token the server has already forgotten.
 */

import { request } from './client';

export interface HouseholdSummary {
  id: string;
  name: string;
  role: 'owner' | 'member' | 'viewer';
}

export interface Session {
  user_id: string;
  email: string;
  display_name: string;
  csrf_token: string;
  households: HouseholdSummary[];
}

export interface Credentials {
  email: string;
  password: string;
}

export interface Registration extends Credentials {
  display_name: string;
  household_name: string;
}

/**
 * Who is signed in, if anyone.
 *
 * Called on every load: the cookie survives a refresh, the in-memory CSRF token
 * does not, and this is where it comes back. A `401` here is not an error to
 * report — it is the normal answer for a visitor who has not signed in — so
 * callers treat it as "no session" rather than surfacing it.
 */
export function fetchSession(signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/session', { signal });
}

export function login(credentials: Credentials, signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/login', { method: 'POST', body: credentials, signal });
}

/** What registration answers. Notably **not** a `Session`. */
export interface RegistrationAccepted {
  status: 'accepted';
  /**
   * Whether this instance has a mail relay at all — a property of the
   * deployment, identical for every address, and therefore not a hint about the
   * one submitted. It decides which sentence the screen shows next, nothing else.
   */
  email_available: boolean;
}

/**
 * Ask for an account. The answer is the same whether or not the address had one.
 *
 * It used to answer `201` with a session for a free address and `409` for a taken
 * one, which let anybody ask whether an address had an account here. It now
 * answers `202` either way — and, since a session could only be minted on one of
 * the two branches, it no longer returns one at all. The difference is sent to
 * the mailbox, which is the only party entitled to it.
 *
 * So the caller must send the person to the sign-in form afterwards, with the
 * password they just chose. There is nothing to adopt.
 */
export function register(
  payload: Registration,
  signal?: AbortSignal,
): Promise<RegistrationAccepted> {
  return request<RegistrationAccepted>('/auth/register', {
    method: 'POST',
    body: payload,
    signal,
  });
}

/** What this instance can do for somebody who is not signed in. */
export interface AuthCapabilities {
  /** False when no SMTP relay is configured; the screen then offers no link. */
  password_reset: boolean;
}

/**
 * Whether a password can be reset here at all.
 *
 * Asked before the screen renders, so that an instance with no mail relay shows
 * the honest sentence instead of a link that leads to a `503`. The same pattern
 * `GET /v1/providers/capabilities` uses: ask what exists rather than discover it
 * from a failure.
 */
export function authCapabilities(signal?: AbortSignal): Promise<AuthCapabilities> {
  return request<AuthCapabilities>('/auth/capabilities', { signal });
}

/**
 * Ask for a reset link.
 *
 * Answers `202` with a constant body whether or not the address has an account,
 * and a message is sent either way — including one saying there is no account
 * here, so "nothing arrived" is never the answer somebody is left with. The only
 * failures worth branching on are `503` (this instance sends no mail) and `429`.
 */
export function requestPasswordReset(email: string, signal?: AbortSignal): Promise<void> {
  return request<void>('/auth/password/reset-request', {
    method: 'POST',
    body: { email },
    signal,
  });
}

export interface PasswordResetCompletion {
  token: string;
  new_password: string;
}

/**
 * Set a new password from a link, and be signed out everywhere.
 *
 * Answers `204` and **no session**: following a link from an inbox proves control
 * of a mailbox, not knowledge of a password, so the person signs in afterwards.
 * Every session of the account is revoked server-side, which is what makes a
 * reset a remedy for a compromise rather than only a convenience.
 */
export function completePasswordReset(
  payload: PasswordResetCompletion,
  signal?: AbortSignal,
): Promise<void> {
  return request<void>('/auth/password/reset', { method: 'POST', body: payload, signal });
}

export function logout(signal?: AbortSignal): Promise<void> {
  return request<void>('/auth/logout', { method: 'POST', signal });
}

export interface PasswordChange {
  current_password: string;
  new_password: string;
}

/**
 * End every session of this account and start a fresh one here.
 *
 * "Sign me out everywhere", for a cookie somebody thinks has been copied. It does
 * not touch this household's machine tokens: those are a separate credential with
 * their own list and their own revocation, and cutting a household's integrations
 * because one person lost a laptop is a surprise nobody asked for.
 */
export function revokeAllSessions(signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/sessions/revoke-all', { method: 'POST', signal });
}

/**
 * Replace the password, having proved the current one.
 *
 * The current password is still required, and the reset flow does not change
 * that: this call is made from a live session, which proves nothing about who is
 * holding it, so skipping the check would turn a stolen cookie into a permanent
 * takeover. Somebody who cannot supply the old password uses
 * `requestPasswordReset` instead, and proves control of the address rather than
 * knowledge of a secret.
 */
export function changePassword(payload: PasswordChange, signal?: AbortSignal): Promise<Session> {
  return request<Session>('/auth/password', { method: 'POST', body: payload, signal });
}
