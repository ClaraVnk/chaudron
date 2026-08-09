import { useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import {
  authCapabilities,
  login,
  register,
  requestPasswordReset,
  type Session,
} from '../../api/auth';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import styles from './Auth.module.css';

type Mode = 'login' | 'register' | 'forgot';

/** The shortest password the server accepts. Quoted so the form can say it first. */
const MIN_PASSWORD_LENGTH = 12;

interface Props {
  onSignedIn: (session: Session) => void;
  /** Set when a session ended under the user rather than at first load. */
  expired?: boolean;
}

/**
 * Sign in, create an account, or ask for a reset link.
 *
 * One screen with three modes rather than three routes: the application has no
 * router, and a person who mistyped their address should be one click from the
 * next thing rather than one navigation.
 *
 * **"Forgot my password" exists now, and it did not.** The old copy on this
 * screen said Chaudron sent no email at all and that the only way back was
 * another owner re-inviting you — which is not a way back when the person locked
 * out *is* the owner. It is shown only when the server says the instance has a
 * mail relay (`GET /v1/auth/capabilities`), because a link that leads to an
 * apology is worse than no link, which was the old copy's own argument.
 *
 * **Registration no longer signs anybody in, and the screen must not pretend
 * otherwise.** The endpoint answers `202` whether or not the address already had
 * an account, so that submitting the form is no longer a way to ask whether
 * somebody has one here. The difference is sent to the mailbox instead. What the
 * screen can honestly say afterwards is therefore the same sentence in both
 * cases, and it is deliberately written to be true in both.
 */
export function AuthScreen({ onSignedIn, expired = false }: Props) {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [householdName, setHouseholdName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetOffered, setResetOffered] = useState(false);

  // Asked once, on mount. A failure leaves `resetOffered` false, which hides the
  // link: an instance we cannot ask about is treated as one that cannot send
  // mail, which is the direction that fails towards an honest screen.
  useEffect(() => {
    const controller = new AbortController();
    authCapabilities(controller.signal)
      .then((capabilities) => {
        setResetOffered(capabilities.password_reset);
      })
      .catch(() => {
        /* No link. See above. */
      });
    return () => {
      controller.abort();
    };
  }, []);

  const clear = () => {
    setError(null);
    setNotice(null);
  };

  const submit = async () => {
    clear();
    setBusy(true);
    try {
      if (mode === 'login') {
        onSignedIn(await login({ email, password }));
      } else if (mode === 'register') {
        const accepted = await register({
          email,
          password,
          display_name: displayName,
          household_name: householdName,
        });
        setNotice(accepted.email_available ? 'registered-with-email' : 'registered-no-email');
        setMode('login');
      } else {
        await requestPasswordReset(email);
        setNotice('reset-sent');
      }
    } catch (cause) {
      setError(messageFor(cause, mode));
    } finally {
      setBusy(false);
    }
  };

  const registering = mode === 'register';
  const forgetting = mode === 'forgot';

  return (
    <main className={styles.screen}>
      <div className={styles.brand}>
        <img src="/icon-192.png" alt="" width={48} height={48} />
        <h1 className={styles.title}>Chaudron</h1>
      </div>

      {expired ? (
        <Callout tone="warn" title="Session terminée">
          Votre session a pris fin. Reconnectez-vous pour reprendre où vous en étiez.
        </Callout>
      ) : null}

      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          // The handler is async and `onSubmit` wants void: floating the promise
          // explicitly says the rejection path is handled inside `submit`.
          event.preventDefault();
          void submit();
        }}
      >
        <h2 className={styles.heading}>{HEADINGS[mode]}</h2>

        {error ? (
          <Callout tone="danger" title={forgetting ? 'Demande impossible' : 'Connexion impossible'}>
            {error}
          </Callout>
        ) : null}

        {notice ? (
          <Callout tone="info" title={NOTICES[notice].title}>
            {NOTICES[notice].body}
          </Callout>
        ) : null}

        <Field label="Adresse e-mail" required>
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="email"
              name="email"
              autoComplete="email"
              inputMode="email"
              autoCapitalize="none"
              spellCheck={false}
              required
              value={email}
              aria-describedby={describedBy}
              aria-invalid={invalid || undefined}
              onChange={(event) => {
                setEmail(event.target.value);
              }}
            />
          )}
        </Field>

        {forgetting ? null : (
          <Field
            label="Mot de passe"
            required
            hint={registering ? `Au moins ${String(MIN_PASSWORD_LENGTH)} caractères.` : undefined}
          >
            {({ id, describedBy, invalid }) => (
              <input
                id={id}
                className={controlClass(invalid)}
                type="password"
                name="password"
                autoComplete={registering ? 'new-password' : 'current-password'}
                required
                minLength={registering ? MIN_PASSWORD_LENGTH : undefined}
                value={password}
                aria-describedby={describedBy}
                aria-invalid={invalid || undefined}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
              />
            )}
          </Field>
        )}

        {registering ? (
          <>
            <Field label="Votre nom" hint="Affiché dans le foyer.">
              {({ id, describedBy }) => (
                <input
                  id={id}
                  className={controlClass()}
                  type="text"
                  name="display-name"
                  autoComplete="name"
                  value={displayName}
                  aria-describedby={describedBy}
                  onChange={(event) => {
                    setDisplayName(event.target.value);
                  }}
                />
              )}
            </Field>

            <Field label="Nom du foyer" hint="Par exemple « Maison » ou « Coloc rue Verte ».">
              {({ id, describedBy }) => (
                <input
                  id={id}
                  className={controlClass()}
                  type="text"
                  name="household-name"
                  value={householdName}
                  aria-describedby={describedBy}
                  onChange={(event) => {
                    setHouseholdName(event.target.value);
                  }}
                />
              )}
            </Field>
          </>
        ) : null}

        <Button type="submit" variant="primary" block loading={busy}>
          {ACTIONS[mode]}
        </Button>

        {mode === 'login' && resetOffered ? (
          <p className={styles.switch}>
            <button
              type="button"
              className={styles.link}
              onClick={() => {
                setMode('forgot');
                clear();
              }}
            >
              Mot de passe oublié ?
            </button>
          </p>
        ) : null}

        <p className={styles.switch}>
          {registering || forgetting ? 'Vous avez déjà un compte ?' : 'Pas encore de compte ?'}{' '}
          <button
            type="button"
            className={styles.link}
            onClick={() => {
              setMode(registering ? 'login' : 'register');
              clear();
            }}
          >
            {registering ? 'Se connecter' : 'Créer un compte'}
          </button>
          {forgetting ? (
            <>
              {' · '}
              <button
                type="button"
                className={styles.link}
                onClick={() => {
                  setMode('login');
                  clear();
                }}
              >
                Revenir à la connexion
              </button>
            </>
          ) : null}
        </p>
      </form>

      {resetOffered ? null : (
        <p className={styles.note}>
          Cette instance Chaudron n’envoie aucun e-mail : il n’y a donc pas de réinitialisation
          automatique du mot de passe. Demandez au propriétaire de votre foyer de vous réinviter.
        </p>
      )}
    </main>
  );
}

const HEADINGS: Record<Mode, string> = {
  login: 'Se connecter',
  register: 'Créer un compte',
  forgot: 'Réinitialiser le mot de passe',
};

const ACTIONS: Record<Mode, string> = {
  login: 'Se connecter',
  register: 'Créer le compte',
  forgot: 'Envoyer le lien',
};

type Notice = 'registered-with-email' | 'registered-no-email' | 'reset-sent';

/**
 * The three sentences shown after a form that answers `202`.
 *
 * Each is written to be **true whether or not the address already had an
 * account**, which is the whole point: a message that said "your account was
 * created" would be the enumeration oracle again, restated in French. So they say
 * what was done — a message was sent, or would have been — and never what was
 * found.
 */
const NOTICES: Record<Notice, { title: string; body: string }> = {
  'registered-with-email': {
    title: 'Vérifiez votre boîte aux lettres',
    body:
      'Un e-mail vient d’être envoyé à cette adresse. Il vous dira si le compte a été créé, ' +
      'ou si un compte existait déjà — et, dans ce cas, comment y revenir. Vous pouvez ' +
      'maintenant vous connecter.',
  },
  'registered-no-email': {
    title: 'Demande enregistrée',
    body:
      'Vous pouvez maintenant vous connecter avec le mot de passe que vous venez de choisir. ' +
      'Cette instance n’envoie pas d’e-mail : si la connexion échoue, cette adresse avait ' +
      'peut-être déjà un compte, avec un autre mot de passe.',
  },
  'reset-sent': {
    title: 'Vérifiez votre boîte aux lettres',
    body:
      'Si un compte existe pour cette adresse, un lien de réinitialisation vient d’y être ' +
      'envoyé. Il est valable une heure et ne fonctionne qu’une fois. Un e-mail part dans ' +
      'tous les cas, y compris pour vous dire qu’aucun compte n’existe ici.',
  },
};

/** Server problems, turned into something a person can act on. */
function messageFor(cause: unknown, mode: Mode): string {
  if (cause instanceof ApiError) {
    switch (cause.problemType) {
      case 'invalid-credentials':
        return 'Adresse e-mail ou mot de passe incorrect.';
      case 'email-not-configured':
        return (
          'Cette instance Chaudron n’envoie aucun e-mail, il n’y a donc pas de ' +
          'réinitialisation automatique. Demandez au propriétaire de votre foyer de vous ' +
          'réinviter.'
        );
      case 'password-too-weak':
        return `Le mot de passe doit faire au moins ${String(MIN_PASSWORD_LENGTH)} caractères.`;
      case 'invalid-email':
        return 'Cette adresse e-mail n’est pas valide.';
      case 'rate-limited':
        return cause.retryAfterSeconds
          ? `Trop de tentatives. Réessayez dans ${String(Math.ceil(cause.retryAfterSeconds / 60))} minute(s).`
          : 'Trop de tentatives. Réessayez plus tard.';
      case 'validation-failed':
        return mode === 'forgot'
          ? 'Cette adresse e-mail n’est pas valide.'
          : `Le mot de passe doit faire au moins ${String(MIN_PASSWORD_LENGTH)} caractères.`;
      default:
        break;
    }
  }
  return describeError(cause);
}
