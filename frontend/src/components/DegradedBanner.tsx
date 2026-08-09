import { useCapabilities } from '../context/capabilitiesContext';
import { formatDegradedReason } from '../api/types';
import { Button } from './ui';
import styles from './DegradedBanner.module.css';

interface DegradedBannerProps {
  /**
   * Take the reader to the screen that fixes this — the recipes tab, which
   * hosts `features/recipes/ProviderSetup` and therefore the registration form
   * itself.
   *
   * A callback rather than a link because the shell holds the current tab above
   * the session gate (`App.tsx`), so navigation here is a state change and not a
   * URL. Optional so the banner still renders in isolation.
   */
  onConfigureProvider?: () => void;
}

/**
 * Permanent, non-dismissible statement of what is reduced and why.
 *
 * Product requirement, not decoration (api-contract-v1.md, "Capacités du
 * fournisseur"): the user has to learn the limit before attempting the action,
 * not from the error that the attempt produces.
 *
 * **Every sentence here addresses the household, not the operator.** Until this
 * revision the "no provider" case said suggestions would stay unavailable "tant
 * qu'un fournisseur n'est pas renseigné côté serveur", which was true when only
 * somebody with a shell on the host could configure one. It has not been true
 * since `POST /v1/providers` and the consent routes shipped: the household
 * registers its own provider, on a screen one tap away, and revision `0016`'s
 * consent gate means it *must* be the household rather than the operator, since
 * an agreement to send a member's health note to a third party is not one an
 * operator may date on somebody else's behalf. Sending the reader to the server
 * administrator now sends them away from the only person who can act.
 */
export function DegradedBanner({ onConfigureProvider }: DegradedBannerProps) {
  const { capabilities, error } = useCapabilities();

  if (error) {
    return (
      <div className={[styles.banner, styles.offline].join(' ')} role="status">
        <p className={styles.header}>
          <span className={styles.mark} aria-hidden="true">
            ⚠
          </span>
          État du service inconnu
        </p>
        <p>
          Impossible de joindre <code>/v1/providers/capabilities</code>. Les suggestions de recettes
          peuvent échouer.
        </p>
        <p className={styles.note}>{error}</p>
      </div>
    );
  }

  if (!capabilities) return null;

  if (!capabilities.configured) {
    return (
      <div className={styles.banner} role="status">
        <p className={styles.header}>
          <span className={styles.mark} aria-hidden="true">
            ⚠
          </span>
          Aucun fournisseur d’IA configuré
        </p>
        <p>
          L’inventaire fonctionne normalement. Les suggestions de recettes et la lecture d’un ticket
          photographié resteront indisponibles tant que ce foyer n’aura pas enregistré de
          fournisseur — et tant qu’il n’en a pas, rien ne sort de cette instance.
        </p>
        {/* Deliberately conditional. A model running on a machine this household
            controls transmits nothing, and needs no agreement at all — saying
            flatly that consent is required would be a second wrong sentence in
            the place of the one being corrected. */}
        <p>
          C’est à ce foyer de l’enregistrer, pas à l’administrateur du serveur : si le modèle est
          hébergé par un tiers, vous seuls pouvez accepter que vos données lui soient envoyées.
        </p>
        {onConfigureProvider ? (
          <p>
            <Button variant="primary" onClick={onConfigureProvider}>
              Configurer un fournisseur
            </Button>
          </p>
        ) : (
          <p>Ouvrez l’onglet « Recettes » pour l’enregistrer.</p>
        )}
      </div>
    );
  }

  if (!capabilities.degraded) return null;

  const reasons = capabilities.degraded_reasons.map(formatDegradedReason).filter(Boolean);

  return (
    <div className={styles.banner} role="status">
      <p className={styles.header}>
        <span className={styles.mark} aria-hidden="true">
          ⚠
        </span>
        Mode dégradé
      </p>
      {reasons.length > 0 ? (
        <ul className={styles.reasons}>
          {reasons.map((reason, index) => (
            <li key={`${String(index)}-${reason}`}>{reason}</li>
          ))}
        </ul>
      ) : (
        <p>Certaines fonctionnalités sont réduites. Le serveur n’a pas précisé lesquelles.</p>
      )}
      {capabilities.model ? (
        <p className={styles.note}>
          Fournisseur : {capabilities.provider ?? 'inconnu'} · modèle {capabilities.model}
        </p>
      ) : null}
      {/* Every remedy the server attaches to a degradation — change the model,
          re-run the probe, replace the key — is carried out on the same screen,
          so the way there belongs next to the reasons rather than in a sentence
          asking the reader to go and find it. */}
      {onConfigureProvider ? (
        <p>
          <Button onClick={onConfigureProvider}>Changer de fournisseur ou de modèle</Button>
        </p>
      ) : null}
    </div>
  );
}
