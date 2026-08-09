import { useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { completePasswordReset } from '../../api/auth';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import styles from './Auth.module.css';

/** The shortest password the server accepts. Quoted so the form can say it first. */
const MIN_PASSWORD_LENGTH = 12;

interface Props {
  /** The token read out of the address bar by `takeResetToken`, already stripped. */
  token: string;
  /** Leave the reset screen and go back to sign-in. */
  onDone: () => void;
}

/**
 * "Choose a new password", reached from a link in a message.
 *
 * Shown **instead of** everything else, signed in or not, because a person who
 * clicks a reset link has a specific intention and it is not "look at my
 * inventory". `App.tsx` checks for the token before it checks for a session.
 *
 * **It does not sign anybody in.** The server answers `204` and no cookie:
 * following a link from an inbox proves control of a mailbox, not knowledge of a
 * password, and minting a session here would make the message itself a login
 * link. So the screen ends by sending the person to the sign-in form with the
 * password they just chose.
 *
 * It also warns, before the fact rather than after it, that every open session
 * ends — that is what makes a reset a remedy for a stolen cookie rather than only
 * a convenience, and being signed out of a phone is a surprise worth spending one
 * sentence on.
 */
export function PasswordResetScreen({ token, onDone }: Props) {
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const mismatch = confirmation.length > 0 && confirmation !== password;

  const submit = async () => {
    setError(null);
    if (password !== confirmation) {
      setError('Les deux mots de passe ne sont pas identiques.');
      return;
    }
    setBusy(true);
    try {
      await completePasswordReset({ token, new_password: password });
      setDone(true);
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <main className={styles.screen}>
        <div className={styles.brand}>
          <img src="/icon-192.png" alt="" width={48} height={48} />
          <h1 className={styles.title}>Chaudron</h1>
        </div>
        <Callout tone="info" title="Mot de passe changé">
          Toutes vos sessions ont été fermées, sur tous vos appareils. Connectez-vous avec votre
          nouveau mot de passe.
        </Callout>
        <Button type="button" variant="primary" block onClick={onDone}>
          Se connecter
        </Button>
      </main>
    );
  }

  return (
    <main className={styles.screen}>
      <div className={styles.brand}>
        <img src="/icon-192.png" alt="" width={48} height={48} />
        <h1 className={styles.title}>Chaudron</h1>
      </div>

      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <h2 className={styles.heading}>Choisir un nouveau mot de passe</h2>

        {error ? (
          <Callout tone="danger" title="Impossible de changer le mot de passe">
            {error}
          </Callout>
        ) : null}

        <Field
          label="Nouveau mot de passe"
          required
          hint={`Au moins ${String(MIN_PASSWORD_LENGTH)} caractères.`}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              name="new-password"
              autoComplete="new-password"
              required
              minLength={MIN_PASSWORD_LENGTH}
              value={password}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
          )}
        </Field>

        <Field
          label="Confirmer le mot de passe"
          required
          error={mismatch ? 'Les deux mots de passe ne sont pas identiques.' : undefined}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              name="confirm-password"
              autoComplete="new-password"
              required
              value={confirmation}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(event) => {
                setConfirmation(event.target.value);
              }}
            />
          )}
        </Field>

        <Button type="submit" variant="primary" block loading={busy}>
          Changer le mot de passe
        </Button>

        <p className={styles.note}>
          Changer le mot de passe ferme toutes les sessions ouvertes, sur tous vos appareils. Vous
          devrez vous reconnecter partout.
        </p>
      </form>

      <p className={styles.switch}>
        <button type="button" className={styles.link} onClick={onDone}>
          Revenir à la connexion
        </button>
      </p>
    </main>
  );
}

/** Server problems, turned into something a person can act on. */
function messageFor(cause: unknown): string {
  if (cause instanceof ApiError) {
    switch (cause.problemType) {
      case 'password-reset-token-invalid':
        return (
          'Ce lien n’est plus utilisable : il a peut-être expiré, déjà servi, ou été remplacé ' +
          'par une demande plus récente. Demandez-en un nouveau depuis l’écran de connexion.'
        );
      case 'password-too-weak':
      case 'validation-failed':
        return `Le mot de passe doit faire au moins ${String(MIN_PASSWORD_LENGTH)} caractères.`;
      case 'rate-limited':
        return cause.retryAfterSeconds
          ? `Trop de tentatives. Réessayez dans ${String(Math.ceil(cause.retryAfterSeconds / 60))} minute(s).`
          : 'Trop de tentatives. Réessayez plus tard.';
      default:
        break;
    }
  }
  return describeError(cause);
}
