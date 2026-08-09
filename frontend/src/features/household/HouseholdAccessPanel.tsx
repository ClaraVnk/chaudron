import { useState, type FormEvent } from 'react';
import { ApiError, describeError } from '../../api/client';
import { fetchSession } from '../../api/auth';
import type { HouseholdInvitationCreated, MembershipRole } from '../../api/types';
import { Badge, Button, Callout, Field, LoadingRow, Radio } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useSession } from '../../context/sessionContext';
import { useHouseholdAccess } from './useHouseholdAccess';
import styles from './Household.module.css';

/** What each role actually authorises, said the way the server enforces it. */
const ROLE_LABELS: Record<MembershipRole, string> = {
  owner: 'Propriétaire',
  member: 'Membre',
  viewer: 'Lecture seule',
};

const ROLE_DETAILS: Record<Exclude<MembershipRole, 'owner'>, string> = {
  member:
    'Peut tout faire au quotidien : ajouter du stock, tenir la liste de courses, saisir les régimes et allergies, demander des recettes.',
  viewer:
    'Peut tout consulter et ne peut rien modifier. Ne peut pas non plus demander de recette : chaque suggestion est un appel de modèle facturé au foyer.',
};

const EXPIRY_CHOICES: { value: string; days: number; label: string }[] = [
  { value: '1', days: 1, label: '24 heures' },
  { value: '7', days: 7, label: '7 jours' },
  { value: '30', days: 30, label: '30 jours' },
];

const DATE_FORMAT: Intl.DateTimeFormatOptions = {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
};

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('fr-CH', DATE_FORMAT);
}

/**
 * Who may sign in to this household, and how somebody else is let in.
 *
 * Distinct from the members list at the top of this screen, and the distinction
 * is the one thing this panel has to make unmistakable: that list is **who
 * eats** — a six-month-old, a grandmother who comes on Sundays, people who will
 * never have an account. This one is **who has a key**.
 *
 * Four rules shape it, and all four are enforced by the server; this panel
 * states them rather than implementing them.
 *
 * **The code is shown once.** The creation response is the only one that carries
 * it, and no route returns it afterwards. Chaudron sends no email, so the code is
 * handed over however the household already talks to each other — read out loud,
 * typed into a message. The panel says so plainly instead of pretending an
 * invitation was "sent".
 *
 * **Only the owner may invite.** An invitation creates a *peer*, who keeps their
 * access after the person who invited them has gone, and who can read every
 * eater's allergens and infant age bands. A member sees this panel read-only.
 *
 * **An invitation never grants ownership.** It is not in the form because it is
 * not in the API and not in the database: giving somebody ownership cannot be
 * undone by the person who gave it, and it is not a thing a pasted code should
 * do.
 *
 * **Anybody can leave; only the owner can remove somebody else; and the last
 * owner cannot leave at all.** That last refusal is deliberate rather than
 * missing — what becomes of a household with nobody in it has not been decided,
 * and erasing the household is a separate, explicit button.
 */
export function HouseholdAccessPanel() {
  const state = useHouseholdAccess();
  const { activeHousehold, adopt } = useSession();
  const isOwner = activeHousehold?.role === 'owner';

  const [inviting, setInviting] = useState(false);
  const [role, setRole] = useState<Exclude<MembershipRole, 'owner'>>('member');
  const [expiry, setExpiry] = useState('7');
  const [saving, setSaving] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [issued, setIssued] = useState<HouseholdInvitationCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [status, setStatus] = useState('');

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);
    const chosen = EXPIRY_CHOICES.find((choice) => choice.value === expiry);
    setSaving(true);
    state
      .invite({ role, expires_in_days: chosen?.days ?? null })
      .then((created) => {
        setIssued(created);
        setCopied(false);
        setCopyFailed(false);
        setInviting(false);
        setStatus('Le code est créé. Copiez-le maintenant : il ne sera plus jamais affiché.');
      })
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.problemType === 'invitation-limit-reached') {
          setSubmitError(
            'Ce foyer a déjà le nombre maximum d’invitations en attente. Révoquez-en une avant d’en créer une autre.',
          );
          return;
        }
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  const copy = (value: string) => {
    // `navigator.clipboard` is absent outside a secure context and can be
    // refused by permission. The value is in a readable field either way, so a
    // failure is told plainly rather than swallowed into a button that did
    // nothing.
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
        setCopyFailed(false);
        setStatus('Le code est copié dans le presse-papiers.');
      })
      .catch(() => {
        setCopied(false);
        setCopyFailed(true);
      });
  };

  const revoke = (id: string) => {
    setBusy(id);
    setSubmitError(null);
    state
      .revokeInvitation(id)
      .then(() => {
        setStatus('L’invitation est révoquée. Le code ne fonctionne plus.');
      })
      .catch((cause: unknown) => {
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setBusy(null);
      });
  };

  const remove = (userId: string, name: string, self: boolean) => {
    setBusy(userId);
    setSubmitError(null);
    state
      .removeMember(userId)
      .then(async () => {
        setConfirming(null);
        setStatus(self ? 'Vous avez quitté ce foyer.' : `${name} n’a plus accès à ce foyer.`);
        // Leaving changes which households this account may open, and the shell
        // reads that from the session. Re-reading it here is what makes the
        // picker — and `X-Household-Id` — agree with the server immediately
        // rather than at the next reload.
        if (self) adopt(await fetchSession());
      })
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.problemType === 'household-would-have-no-owner') {
          setSubmitError(
            'Vous êtes le seul propriétaire de ce foyer : le quitter le laisserait sans personne pour l’administrer. Nommez d’abord quelqu’un d’autre, ou supprimez le foyer depuis vos données personnelles.',
          );
          return;
        }
        setSubmitError(describeError(cause));
      })
      .finally(() => {
        setBusy(null);
      });
  };

  if (state.unsupported) return null;

  return (
    <section className={styles.card} aria-labelledby="household-access-heading">
      <div className={styles.cardHead}>
        <h2 className={styles.formHeading} id="household-access-heading">
          Accès au foyer
        </h2>
        {state.members.length > 0 ? <Badge tone="neutral">{state.members.length}</Badge> : null}
      </div>

      <p className={styles.lead}>
        Les comptes qui peuvent ouvrir ce foyer. À ne pas confondre avec les membres plus haut :
        ceux-là sont les personnes qui mangent ici — un nourrisson, quelqu’un qui n’aura jamais de
        compte. Ici, ce sont les clés.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {issued !== null ? (
        <div className={styles.tokenReveal} aria-live="assertive">
          <Callout tone="warn" title="Copiez ce code maintenant">
            <p>
              C’est la seule fois où il s’affiche. Chaudron n’en garde qu’une empreinte : ni cet
              écran ni l’API ne pourront vous le remontrer. Transmettez-le à la personne directement
              — Chaudron n’envoie aucun courriel. S’il se perd, révoquez-le et créez-en un autre.
            </p>
          </Callout>

          <Field label={`Code d’invitation (${ROLE_LABELS[issued.role].toLowerCase()})`}>
            {({ id, describedBy }) => (
              <input
                id={id}
                className={controlClass()}
                type="text"
                readOnly
                value={issued.token}
                aria-describedby={describedBy}
                spellCheck={false}
                onFocus={(event) => {
                  event.target.select();
                }}
              />
            )}
          </Field>

          {copyFailed ? (
            <p className={styles.consentNote}>
              La copie automatique a été refusée par le navigateur. Sélectionnez le champ ci-dessus
              et copiez-le à la main.
            </p>
          ) : null}

          <div className={styles.cardActions}>
            <Button
              variant="primary"
              onClick={() => {
                copy(issued.token);
              }}
            >
              {copied ? 'Copié' : 'Copier le code'}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setIssued(null);
                setCopied(false);
                setCopyFailed(false);
              }}
            >
              J’ai copié le code, masquer
            </Button>
          </div>
        </div>
      ) : null}

      {state.loading && state.members.length === 0 ? (
        <LoadingRow label="Chargement des accès…" />
      ) : state.error !== null ? (
        <Callout tone="warn" title="Impossible de lire les accès">
          <p>{state.error}</p>
          <div className={styles.cardActions}>
            <Button variant="secondary" onClick={state.reload}>
              Réessayer
            </Button>
          </div>
        </Callout>
      ) : (
        <ul className={styles.list}>
          {state.members.map((member) => (
            <li key={member.user_id} className={styles.tokenRow}>
              <div className={styles.cardHead}>
                <span className={styles.cardName}>
                  {member.display_name}
                  {member.is_self ? <span className={styles.cardBand}> vous</span> : null}
                </span>
                <Badge tone="neutral">{ROLE_LABELS[member.role]}</Badge>
              </div>

              <div className={styles.cardFacts}>
                <span className={styles.cardLine}>
                  <span className={styles.cardLabel}>Compte</span>
                  {member.email}
                </span>
                <span className={styles.cardLine}>
                  <span className={styles.cardLabel}>Membre depuis</span>
                  {formatDate(member.joined_at)}
                </span>
              </div>

              {isOwner || member.is_self ? (
                confirming === member.user_id ? (
                  <div className={styles.cardActions}>
                    <Button
                      variant="danger"
                      loading={busy === member.user_id}
                      onClick={() => {
                        remove(member.user_id, member.display_name, member.is_self);
                      }}
                    >
                      {member.is_self ? 'Quitter ce foyer' : 'Retirer l’accès'}
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setConfirming(null);
                      }}
                    >
                      Annuler
                    </Button>
                  </div>
                ) : (
                  <div className={styles.cardActions}>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setSubmitError(null);
                        setConfirming(member.user_id);
                      }}
                    >
                      {member.is_self ? 'Quitter ce foyer' : 'Retirer l’accès'}
                      {member.is_self ? null : (
                        <span className="visually-hidden"> de {member.display_name}</span>
                      )}
                    </Button>
                  </div>
                )
              ) : null}
            </li>
          ))}
        </ul>
      )}

      {submitError !== null ? (
        <Callout tone="danger" title="Action impossible">
          <p>{submitError}</p>
        </Callout>
      ) : null}

      {!isOwner ? (
        <p className={styles.consentNote}>
          Seul le propriétaire du foyer peut inviter quelqu’un. Une invitation donne accès aux
          allergies et aux âges des personnes enregistrées ici, et la personne invitée reste après
          le départ de celle qui l’a invitée.
        </p>
      ) : state.invitations.length > 0 ? (
        <>
          <h3 className={styles.formHeading}>Invitations en attente</h3>
          <p className={styles.consentNote}>
            Chacune est une porte ouverte tant qu’elle n’a pas servi ou expiré. Elle ne peut servir
            qu’une seule fois.
          </p>
          <ul className={styles.list}>
            {state.invitations.map((invitation) => (
              <li key={invitation.id} className={styles.tokenRow}>
                <div className={styles.cardHead}>
                  <code className={styles.tokenTail}>
                    {invitation.prefix}…{invitation.last4}
                  </code>
                  <Badge tone="neutral">{ROLE_LABELS[invitation.role]}</Badge>
                </div>
                <div className={styles.cardFacts}>
                  <span className={styles.cardLine}>
                    <span className={styles.cardLabel}>Créée le</span>
                    {formatDate(invitation.created_at)}
                  </span>
                  <span className={styles.cardLine}>
                    <span className={styles.cardLabel}>Expire le</span>
                    {formatDate(invitation.expires_at)}
                  </span>
                </div>
                <div className={styles.cardActions}>
                  <Button
                    variant="danger"
                    loading={busy === invitation.id}
                    onClick={() => {
                      revoke(invitation.id);
                    }}
                  >
                    Révoquer
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {isOwner ? (
        inviting ? (
          <form className={styles.form} onSubmit={submit} noValidate>
            <fieldset className={styles.roleChoice}>
              <legend className={styles.formHeading}>Ce que la personne pourra faire</legend>
              {(['member', 'viewer'] as const).map((candidate) => (
                <Radio
                  key={candidate}
                  name="invitation-role"
                  value={candidate}
                  checked={role === candidate}
                  onSelect={() => {
                    setRole(candidate);
                  }}
                  detail={ROLE_DETAILS[candidate]}
                >
                  {ROLE_LABELS[candidate]}
                </Radio>
              ))}
            </fieldset>

            <p className={styles.consentNote}>
              Il n’y a pas d’option « propriétaire » : donner la propriété ne peut pas être annulé
              par la personne qui la donne, et cela ne se fait pas en collant un code.
            </p>

            <Field
              label="Validité du code"
              hint="Passé ce délai, le code est refusé comme un code inconnu. Vous pouvez le révoquer avant."
            >
              {({ id, describedBy, invalid }) => (
                <select
                  id={id}
                  className={controlClass(invalid)}
                  value={expiry}
                  aria-describedby={describedBy}
                  onChange={(event) => {
                    setExpiry(event.target.value);
                  }}
                >
                  {EXPIRY_CHOICES.map((choice) => (
                    <option key={choice.value} value={choice.value}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              )}
            </Field>

            {submitError !== null ? (
              <Callout tone="danger" title="Création impossible">
                <p>{submitError}</p>
              </Callout>
            ) : null}

            <div className={styles.formActions}>
              <Button type="submit" variant="primary" loading={saving}>
                Créer le code
              </Button>
              <Button
                variant="ghost"
                onClick={() => {
                  setInviting(false);
                  setSubmitError(null);
                }}
              >
                Annuler
              </Button>
            </div>
          </form>
        ) : (
          <Button
            variant="secondary"
            block
            onClick={() => {
              setSubmitError(null);
              setInviting(true);
            }}
          >
            Inviter quelqu’un
          </Button>
        )
      ) : null}
    </section>
  );
}
