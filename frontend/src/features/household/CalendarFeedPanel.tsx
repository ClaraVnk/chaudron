import { useEffect, useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { getCalendarSubscription, revokeCalendarSubscription } from '../../api/endpoints';
import type { CalendarSubscription } from '../../api/types';
import { Badge, Button, Callout, LoadingRow } from '../../components/ui';
import { useSession } from '../../context/sessionContext';
import styles from './Household.module.css';

/**
 * The CalDAV account details, and the one gesture that makes them work.
 *
 * **This panel exists because the feature did not, from where a user stands.**
 * The server has spoken full CalDAV all along — `PROPFIND`, `REPORT`,
 * `/.well-known/caldav`, and `GET /v1/calendar/subscription` returning
 * everything to type — and no screen ever called it. The landing page
 * advertised expiry reminders on the phone, the application offered no address,
 * and the owner reported on 2026-08-17 that it simply could not be done.
 *
 * **The instruction is the feature.** Apple exposes two doors and a subscribed
 * calendar is not one of them: a subscription carries `VEVENT` only and drops
 * every `VTODO` *without a message* (README, "There is no Reminders API"). So
 * the obvious gesture — Calendar › add a subscribed calendar, paste a URL —
 * produces a silent nothing, which is exactly what was reported. Naming the
 * right path is therefore not documentation politeness; it is the difference
 * between the feature working and appearing broken.
 *
 * **The password is shown, not hidden behind a reveal.** It is re-derived per
 * request and never stored, so there is nothing to protect it from that hiding
 * it would achieve — and a credential you must retype into a phone by hand is a
 * credential you have to be able to read.
 */
export function CalendarFeedPanel() {
  const { activeHousehold } = useSession();
  // The route is owner-only server-side, because this credential outlives a
  // membership: it keeps working until somebody revokes it on purpose. Hiding
  // the panel is a courtesy; the refusal that matters is the server's.
  const isOwner = activeHousehold?.role === 'owner';

  const [feed, setFeed] = useState<CalendarSubscription | null>(null);
  // Derived rather than set, because a member never issues the request at all
  // and writing `false` into state from the effect is both a lint error and a
  // needless second render.
  const [pending, setPending] = useState(true);
  const loading = isOwner && pending;
  // `unsupported` is a normal state, not a failure: an instance can run with the
  // calendar feed switched off, and then this panel says nothing at all rather
  // than showing an error for a feature nobody asked for.
  const [unsupported, setUnsupported] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [status, setStatus] = useState('');

  useEffect(() => {
    if (!isOwner) return;
    const controller = new AbortController();
    getCalendarSubscription(controller.signal)
      .then((value) => {
        setFeed(value);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (cause instanceof ApiError && (cause.status === 404 || cause.status === 501)) {
          setUnsupported(true);
          return;
        }
        setError(describeError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPending(false);
      });
    return () => {
      controller.abort();
    };
  }, [isOwner]);

  const revoke = (): void => {
    setRevoking(true);
    setError(null);
    revokeCalendarSubscription()
      .then((value) => {
        setFeed(value);
        setStatus(
          'Nouveaux identifiants créés. Les appareils déjà configurés ne se synchronisent plus.',
        );
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setRevoking(false);
      });
  };

  if (unsupported) return null;

  return (
    <section className={styles.card} aria-labelledby="calendar-feed-heading">
      <div className={styles.cardHead}>
        <h2 className={styles.formHeading} id="calendar-feed-heading">
          Rappels de péremption sur le téléphone
        </h2>
        {feed !== null ? <Badge tone="ok">Actif</Badge> : null}
      </div>

      <p className={styles.lead}>
        Chaudron publie ce qui périme sous forme de <strong>tâches</strong>, que le téléphone range
        dans Rappels et non dans l’agenda. En lecture seule : cocher une tâche là-bas ne change rien
        ici.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {!isOwner ? (
        <Callout tone="info" title="Réservé au propriétaire du foyer">
          <p>
            Ces identifiants continuent de fonctionner même si votre accès au foyer s’arrête, donc
            seul le propriétaire peut les voir et les remplacer. Demandez-les-lui.
          </p>
        </Callout>
      ) : null}

      {loading ? <LoadingRow label="Chargement des identifiants…" /> : null}

      {error !== null ? (
        <Callout tone="warn" title="Impossible de lire les identifiants">
          <p>{error}</p>
        </Callout>
      ) : null}

      {feed !== null ? (
        <>
          {/* The path, before the values. Someone who pastes these into the wrong
              iOS screen gets silence rather than an error, so the wrong screen
              has to be ruled out first. */}
          <Callout tone="info" title="Ajoutez un compte, pas un abonnement">
            <p>
              Sur iPhone :{' '}
              <strong>
                Réglages › Apps › Calendrier › Comptes › Ajouter un compte › Autre › Ajouter un
                compte CalDAV
              </strong>
              , puis les trois valeurs ci-dessous. Les rappels apparaissent ensuite dans l’app
              Rappels.
            </p>
            <p>
              « S’abonner à un calendrier » ne marchera pas : un abonnement ne transporte que des
              événements et jette les tâches sans rien afficher.
            </p>
          </Callout>

          <dl className={styles.feedFacts}>
            <dt>Serveur</dt>
            <dd>
              <code>{feed.server_url}</code>
            </dd>
            <dt>Nom d’utilisateur</dt>
            <dd>
              <code>{feed.username}</code>
            </dd>
            <dt>Mot de passe</dt>
            <dd>
              <code>{feed.password}</code>
            </dd>
          </dl>

          <p className={styles.hint}>
            Le calendrier montre ce qui périme entre {feed.window_days_past} jour
            {feed.window_days_past > 1 ? 's' : ''} en arrière et {feed.window_days_future} jours en
            avant, dans la limite de {feed.max_tasks} tâches.
          </p>

          <div className={styles.cardActions}>
            <Button variant="secondary" onClick={revoke} disabled={revoking}>
              {revoking ? 'Remplacement…' : 'Remplacer les identifiants'}
            </Button>
          </div>
          <p className={styles.hint}>
            À faire si ce mot de passe a été vu par quelqu’un d’autre. Tous les appareils déjà
            configurés cessent alors de se synchroniser et doivent recevoir les nouveaux à la main.
            Personne n’est déconnecté et le stock ne change pas.
          </p>
        </>
      ) : null}
    </section>
  );
}
