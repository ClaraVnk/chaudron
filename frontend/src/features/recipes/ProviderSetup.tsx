import type { ProviderCapabilities } from '../../api/types';
import styles from './Recipes.module.css';

/**
 * Shown instead of a raw error whenever the household has no usable provider —
 * either because `GET /v1/providers/capabilities` said `configured: false`, or
 * because `POST /v1/recipes/suggest` answered 409 provider-not-configured.
 *
 * The contract requires a configuration screen here. A 409 rendered as
 * "Erreur 409" tells the user nothing they can act on.
 */
export function ProviderSetup({ capabilities }: { capabilities: ProviderCapabilities | null }) {
  return (
    <div className={styles.setup}>
      <h2 className={styles.title}>Suggestions de recettes non configurées</h2>
      <p className={styles.lead}>
        Le reste de Chaudron fonctionne : l’inventaire, le scan et l’ajout d’articles ne dépendent
        d’aucun fournisseur d’IA. Seules les suggestions de recettes en ont besoin.
      </p>
      <p className={styles.sectionTitle}>Ce qu’il reste à faire, côté serveur</p>
      <ol className={styles.setupList}>
        <li>
          Choisir un mode : <code>instance_owner</code> (les clés de l’exploitant, réservées à son
          propre foyer) ou <code>byok</code> (chaque foyer apporte les siennes).
        </li>
        <li>
          Renseigner la configuration du fournisseur dans l’environnement du backend, puis
          redémarrer le service.
        </li>
        <li>
          Recharger cette page : l’état est relu au démarrage via{' '}
          <code>GET /v1/providers/capabilities</code>.
        </li>
      </ol>
      {capabilities ? (
        <p className={styles.summary}>
          État actuel : mode {capabilities.mode ?? 'non défini'}, fournisseur{' '}
          {capabilities.provider ?? 'non défini'}.
        </p>
      ) : null}
      <p className={styles.summary}>
        Aucune clé de fournisseur ne transite par cette interface, et aucune n’est stockée dans le
        navigateur.
      </p>
    </div>
  );
}
