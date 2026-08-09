import { useState, type FormEvent } from 'react';
import { ApiError, describeError } from '../../api/client';
import { fetchSession } from '../../api/auth';
import { redeemHouseholdInvitation } from '../../api/endpoints';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useSession } from '../../context/sessionContext';
import styles from './Household.module.css';

/**
 * The other end of an invitation: pasting the code and joining the household.
 *
 * Collapsed by default, because most people open this screen for another reason
 * and a permanently-open "join a household" form on a settings page reads like
 * something is missing.
 *
 * **This is the only screen that does not send `X-Household-Id`.** It cannot: the
 * account is not a member of the household it is about to join, so there is
 * nothing for the server to check the header against. The code is what decides
 * which household this is (`POST /v1/auth/invitations/redeem`), which is why the
 * call lives under `/auth` and why joining changes the household picker rather
 * than the current screen.
 *
 * After a successful redemption the session is re-read and adopted. Without
 * that, the new household would exist on the server and be invisible here until
 * the next reload — and the picker is the only way to reach it.
 *
 * Every refusal about the code itself is one message, deliberately: the server
 * cannot distinguish "never existed" from "expired", "revoked" or "already
 * used", and inventing a distinction the API refuses to make would be inventing
 * it wrong.
 *
 * **Two call sites, and the difference between them is read off the session
 * rather than passed in.** The panel sits on the household settings screen,
 * where it is one option among several and starts collapsed; it also sits on the
 * "no household" screen in `App.tsx`, where it is the *only* thing to do and so
 * starts open with its own framing. Deriving that from
 * `session.households.length` rather than from a prop means the two call sites
 * cannot disagree with what is actually true of the account — an account with no
 * membership can never be told it will "keep its current household".
 *
 * That second call site is what makes an invitation redeemable at all for
 * somebody with no household. This panel used to live only inside
 * `HouseholdScreen`, which the shell reaches only once a household is active, so
 * an account with zero memberships — an owner who left a household that still
 * had another owner, which is reachable today — had a screen with nothing on it
 * but *sign out*, and no way to paste the code that would let them back in.
 */
export function JoinHouseholdPanel() {
  const { session, adopt, selectHousehold } = useSession();
  // An account with no membership has nothing else it could be doing here, so
  // the form is already open: one less tap on the one screen where the tap has
  // no alternative.
  const withoutHousehold = session !== null && session.households.length === 0;
  const [open, setOpen] = useState(withoutHousehold);
  const [code, setCode] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joined, setJoined] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    const cleaned = code.trim();
    if (cleaned === '') {
      setError('Collez le code que la personne qui vous invite vous a transmis.');
      return;
    }

    setSaving(true);
    redeemHouseholdInvitation(cleaned)
      .then(async (result) => {
        // In this order: adopt the session first, so the household is one the
        // picker knows about, then select it. Selecting an unknown household is
        // ignored on purpose (`SessionProvider.selectHousehold`).
        adopt(await fetchSession());
        selectHousehold(result.household_id);
        setJoined(result.household_name);
        setCode('');
        setOpen(false);
      })
      .catch((cause: unknown) => {
        if (cause instanceof ApiError && cause.problemType === 'already-a-member') {
          setError(
            'Vous faites déjà partie de ce foyer. Choisissez-le dans le sélecteur de foyer.',
          );
          return;
        }
        if (cause instanceof ApiError && cause.problemType === 'invitation-not-accepted') {
          setError(
            'Ce code n’est pas utilisable. Il a peut-être expiré, été révoqué, ou déjà servi — un code ne sert qu’une fois. Demandez-en un nouveau à la personne qui vous invite.',
          );
          return;
        }
        setError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  return (
    <section className={styles.card} aria-labelledby="join-household-heading">
      <h2 className={styles.formHeading} id="join-household-heading">
        Rejoindre un foyer
      </h2>

      <p className={styles.lead}>
        {withoutHousehold
          ? 'Si quelqu’un vous a transmis un code d’invitation, collez-le ici : vous rejoindrez son foyer et retrouverez vos écrans immédiatement, sans créer de compte supplémentaire.'
          : 'Si quelqu’un vous a transmis un code d’invitation, collez-le ici. Vous garderez votre compte et votre foyer actuel : un compte peut appartenir à plusieurs foyers, et le sélecteur en haut de l’écran sert à passer de l’un à l’autre.'}
      </p>

      {joined !== null ? (
        <Callout tone="info" title="Vous avez rejoint le foyer">
          <p>
            Vous faites maintenant partie de « {joined} ». Utilisez le sélecteur de foyer pour
            passer de l’un à l’autre.
          </p>
        </Callout>
      ) : null}

      {open ? (
        <form className={styles.form} onSubmit={submit} noValidate>
          <Field
            label="Code d’invitation"
            required
            error={error}
            hint="Un code ne sert qu’une seule fois et expire. Il commence par « chdi_ »."
          >
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                className={controlClass(invalid)}
                type="text"
                autoComplete="off"
                spellCheck={false}
                maxLength={256}
                value={code}
                aria-describedby={describedBy}
                onChange={(event) => {
                  setCode(event.target.value);
                }}
              />
            )}
          </Field>

          <div className={styles.formActions}>
            <Button type="submit" variant="primary" loading={saving}>
              Rejoindre
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setOpen(false);
                setError(null);
                setCode('');
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
            setJoined(null);
            setError(null);
            setOpen(true);
          }}
        >
          J’ai un code d’invitation
        </Button>
      )}
    </section>
  );
}
