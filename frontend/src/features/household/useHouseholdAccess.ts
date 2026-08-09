import { useCallback, useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import {
  createHouseholdInvitation,
  getHouseholdAccess,
  getHouseholdInvitations,
  removeHouseholdAccess,
  revokeHouseholdInvitation,
} from '../../api/endpoints';
import type {
  HouseholdAccess,
  HouseholdInvitation,
  HouseholdInvitationCreated,
  HouseholdInvitationDraft,
} from '../../api/types';

/**
 * Who may open this household, and which invitations are still usable.
 *
 * Two lists, loaded together, because they answer one question — "who can get
 * in?" — and a screen that showed the members without the pending invitations
 * would answer it wrongly by exactly the number of codes lying around.
 *
 * `create` is the one mutation here whose **return value matters**, for the same
 * reason as `useMachineTokens.create`: the server's answer carries the
 * invitation itself and nothing will ever return it again. It is handed straight
 * back to the caller and this module keeps no copy.
 *
 * The invitations list is owner-only on the server. A `403` there is therefore a
 * *role*, not a failure, and it leaves `invitations` empty with no error shown —
 * a member looking at this screen sees the membership list and no reason to
 * think something is broken.
 */

/** Stable identities: consumers put these arrays in memo dependencies. */
const NO_MEMBERS: HouseholdAccess[] = [];
const NO_INVITATIONS: HouseholdInvitation[] = [];

interface Result {
  nonce: number;
  members: HouseholdAccess[];
  invitations: HouseholdInvitation[];
  error: string | null;
  unsupported: boolean;
}

export interface HouseholdAccessState {
  members: HouseholdAccess[];
  invitations: HouseholdInvitation[];
  loading: boolean;
  error: string | null;
  /** The server predates this feature and has no /v1/households/members route. */
  unsupported: boolean;
  reload: () => void;
  /** Resolves with the invitation, **including its one and only value**. */
  invite: (draft: HouseholdInvitationDraft) => Promise<HouseholdInvitationCreated>;
  revokeInvitation: (id: string) => Promise<void>;
  removeMember: (userId: string) => Promise<void>;
}

function isMissingRoute(cause: unknown): boolean {
  return cause instanceof ApiError && (cause.status === 404 || cause.status === 501);
}

export function useHouseholdAccess(): HouseholdAccessState {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  const reload = useCallback(() => {
    setNonce((value) => value + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    const invitations = getHouseholdInvitations(controller.signal).catch((cause: unknown) => {
      // 403 is "you are not the owner", which is a normal state for this screen.
      if (cause instanceof ApiError && cause.status === 403) return NO_INVITATIONS;
      if (isMissingRoute(cause)) return NO_INVITATIONS;
      throw cause;
    });

    Promise.all([getHouseholdAccess(controller.signal), invitations])
      .then(([members, pending]) => {
        setResult({ nonce, members, invitations: pending, error: null, unsupported: false });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        const missing = isMissingRoute(cause);
        setResult({
          nonce,
          members: NO_MEMBERS,
          invitations: NO_INVITATIONS,
          error: missing ? null : describeError(cause),
          unsupported: missing,
        });
      });

    return () => {
      controller.abort();
    };
  }, [nonce]);

  const invite = useCallback(
    async (draft: HouseholdInvitationDraft) => {
      const created = await createHouseholdInvitation(draft);
      reload();
      return created;
    },
    [reload],
  );

  const revokeInvitation = useCallback(
    async (id: string) => {
      await revokeHouseholdInvitation(id);
      reload();
    },
    [reload],
  );

  const removeMember = useCallback(
    async (userId: string) => {
      await removeHouseholdAccess(userId);
      reload();
    },
    [reload],
  );

  return {
    members: result?.members ?? NO_MEMBERS,
    invitations: result?.invitations ?? NO_INVITATIONS,
    loading: result?.nonce !== nonce,
    error: result?.error ?? null,
    unsupported: result?.unsupported ?? false,
    reload,
    invite,
    revokeInvitation,
    removeMember,
  };
}
