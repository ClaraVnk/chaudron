"""The four messages this application sends, and the link they carry.

Four, and no more. Each exists because a person is waiting on an answer that
cannot be given in an HTTP response without also giving it to whoever asked:

* :func:`account_created` -- "the account you just asked for exists".
* :func:`address_already_registered` -- "somebody tried to register your
  address; you already have an account, and here is how to get back into it".
* :func:`password_reset` -- "you asked to reset; here is the link".
* :func:`password_reset_no_account` -- "you asked to reset an address that has no
  account here".

The first two are the pair that closes the registration oracle (pentest finding
O-10f). ``POST /v1/auth/register`` answers ``202`` for both cases and the
*difference travels by mail*, to the mailbox, which is the only party entitled to
know whether the address has an account. The last two are the same trick applied
to ``POST /v1/auth/password/reset-request``.

**The fourth one is not padding.** An address with no account could simply be sent
nothing, and the API response would still be uniform -- but then a person who
mistyped their address on the reset form waits forever for a message that was
never going to arrive, and ``docs/security-model.md`` names precisely that
outcome as the thing a mail feature must not produce: *"a send that fails
silently turns 'I did not receive the message' into an unsolvable incident"*. It
costs one message to a mailbox whose owner learns nothing they did not already
know: that they have no account here.

**The text is French**, like every other string this application shows a person
(``services/providers.py``). Identifiers, comments and log lines stay English.

**No message ever quotes a password, a session, or a household's contents.** The
reset link is the only secret any of them carries, and it is single-use, short
lived and revocable (``services/auth.py``).
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote

from chaudron.domain.email_ports import OutboundMessage

__all__ = [
    "RESET_TOKEN_QUERY_PARAM",
    "account_created",
    "address_already_registered",
    "password_reset",
    "password_reset_no_account",
    "reset_link",
]

#: The query parameter the interface reads the token back out of. A query
#: parameter on the root rather than a path segment because the PWA has no router
#: and is served as a single document; a path would need a rewrite rule in
#: whatever serves it, and a rule that is missing turns every reset link into a
#: 404 the operator discovers from a user.
#:
#: The interface strips it from the address bar with ``history.replaceState`` on
#: the first render, so the token does not sit in the browser's history or ride
#: out on a ``Referer``. That is belt and braces over the properties that actually
#: bound it: one use, one hour, and a digest at rest.
RESET_TOKEN_QUERY_PARAM: Final = "reset"  # noqa: S105 -- a parameter name, not a credential

#: Prefixed on every subject, so a household filtering its mail can key on one
#: string and so a message from this instance is recognisable in a list.
_SUBJECT_PREFIX: Final = "Chaudron"

#: What the messages call the delay, when the delay is a whole number of hours.
#: Sixty minutes reads as "1 heure" rather than "60 minutes", which is how the
#: person reading it thinks about it.
_MINUTES_PER_HOUR: Final = 60


def reset_link(app_url: str, token: str) -> str:
    """The URL a person follows to choose a new password.

    ``app_url`` is where the *interface* is served, which is not necessarily where
    the API is: ``CHAUDRON_PUBLIC_APP_URL`` exists for the deployments where the
    two differ, and falls back to ``CHAUDRON_BASE_URL`` for the common case where
    they do not (``config.py``).

    The token is percent-encoded even though it is hexadecimal and cannot contain
    a character that needs it. The encoding is not for today's generator; it is
    for the day somebody changes it to something URL-safe-base64 and does not
    think about the ``+`` (``services/auth.py`` explains why it is hex).
    """
    return f"{app_url.rstrip('/')}/?{RESET_TOKEN_QUERY_PARAM}={quote(token, safe='')}"


def _validity(ttl_minutes: int) -> str:
    if ttl_minutes % _MINUTES_PER_HOUR == 0:
        hours = ttl_minutes // _MINUTES_PER_HOUR
        return "1 heure" if hours == 1 else f"{hours} heures"
    return f"{ttl_minutes} minutes"


def _signature() -> str:
    return (
        "\n\n--\nCe message est automatique ; personne ne lit les réponses.\n"
        "Chaudron — l'inventaire alimentaire que vous hébergez vous-même."
    )


def account_created(*, to: str, display_name: str, app_url: str) -> OutboundMessage:
    """The address had no account, and now has one.

    Also the closest thing this application has to address verification: a person
    who never receives it registered an address that is not theirs, or mistyped
    it, and either way the account is unreachable and they know to make another.
    Verification proper -- refusing to sign in until the address is confirmed --
    is deliberately **not** built; see ``docs/security-model.md``.
    """
    greeting = f"Bonjour {display_name}," if display_name.strip() else "Bonjour,"
    return OutboundMessage(
        to=to,
        subject=f"{_SUBJECT_PREFIX} — votre compte est prêt",
        body=(
            f"{greeting}\n\n"
            "Votre compte Chaudron a été créé avec cette adresse e-mail.\n"
            f"Vous pouvez vous connecter dès maintenant : {app_url.rstrip('/')}/\n\n"
            "Si vous n'êtes pas à l'origine de cette inscription, ignorez ce message : "
            "sans votre mot de passe, personne ne peut ouvrir ce compte."
            f"{_signature()}"
        ),
    )


def address_already_registered(
    *, to: str, app_url: str, token: str, ttl_minutes: int
) -> OutboundMessage:
    """Somebody submitted the registration form with an address that already has an account.

    The message a person receives instead of the ``409`` the *caller* used to
    receive. It says two things and no more: that an attempt was made, and that
    the way back into an existing account is a reset link -- because the most
    likely author of that attempt is the account's own owner, who has forgotten
    they already signed up.

    It deliberately does **not** say when the account was created, what its
    display name is, or which households it belongs to. Whoever submitted the form
    does not have the mailbox; the person who does already knows.
    """
    return OutboundMessage(
        to=to,
        subject=f"{_SUBJECT_PREFIX} — une inscription a été tentée avec votre adresse",
        body=(
            "Bonjour,\n\n"
            "Quelqu'un vient de demander la création d'un compte Chaudron avec cette "
            "adresse e-mail. Un compte existe déjà : aucun second compte n'a été créé "
            "et rien n'a changé.\n\n"
            "Si c'était vous et que vous avez oublié votre mot de passe, vous pouvez en "
            "choisir un nouveau ici :\n"
            f"{reset_link(app_url, token)}\n\n"
            f"Ce lien est valable {_validity(ttl_minutes)} et ne fonctionne qu'une fois.\n\n"
            "Si ce n'était pas vous, ignorez ce message. Votre mot de passe est inchangé "
            "et vos sessions ouvertes n'ont pas été touchées."
            f"{_signature()}"
        ),
    )


def password_reset(*, to: str, app_url: str, token: str, ttl_minutes: int) -> OutboundMessage:
    """The reset link itself."""
    return OutboundMessage(
        to=to,
        subject=f"{_SUBJECT_PREFIX} — réinitialiser votre mot de passe",
        body=(
            "Bonjour,\n\n"
            "Vous avez demandé à réinitialiser le mot de passe de votre compte Chaudron. "
            "Choisissez-en un nouveau ici :\n"
            f"{reset_link(app_url, token)}\n\n"
            f"Ce lien est valable {_validity(ttl_minutes)} et ne fonctionne qu'une fois. "
            "Une fois le mot de passe changé, toutes vos sessions ouvertes seront fermées, "
            "sur tous vos appareils.\n\n"
            "Si vous n'avez rien demandé, ignorez ce message : tant que ce lien n'est pas "
            "suivi, rien ne change."
            f"{_signature()}"
        ),
    )


def password_reset_no_account(*, to: str, app_url: str) -> OutboundMessage:
    """A reset was asked for an address that has no account on this instance.

    Sent so that "nothing arrived" is never the answer to a reset request. Without
    it, a mistyped address and a broken mail server look identical to the person
    waiting, and only one of the two is their problem to fix.
    """
    return OutboundMessage(
        to=to,
        subject=f"{_SUBJECT_PREFIX} — aucune réinitialisation possible",
        body=(
            "Bonjour,\n\n"
            "Une réinitialisation de mot de passe vient d'être demandée pour cette "
            "adresse e-mail sur Chaudron. Aucun compte n'y est associé, il n'y a donc "
            "rien à réinitialiser.\n\n"
            "Si vous avez un compte, il utilise peut-être une autre adresse. Vous pouvez "
            f"réessayer ici : {app_url.rstrip('/')}/\n\n"
            "Si vous n'avez rien demandé, ignorez ce message."
            f"{_signature()}"
        ),
    )
