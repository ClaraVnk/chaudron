import { useCapabilities } from '../context/capabilitiesContext';
import { formatDegradedReason } from '../api/types';
import styles from './DegradedBanner.module.css';

/**
 * Permanent, non-dismissible statement of what is reduced and why.
 *
 * Product requirement, not decoration (api-contract-v1.md, "Capacités du
 * fournisseur"): the user has to learn the limit before attempting the action,
 * not from the error that the attempt produces.
 */
export function DegradedBanner() {
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
          L’inventaire fonctionne normalement. Les suggestions de recettes resteront indisponibles
          tant qu’un fournisseur n’est pas renseigné côté serveur.
        </p>
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
    </div>
  );
}
