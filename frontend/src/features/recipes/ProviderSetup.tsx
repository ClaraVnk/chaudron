import { formatDegradedReason, type ProviderCapabilities } from '../../api/types';
import { ProviderConfigPanel } from '../providers/ProviderConfigPanel';
import styles from './Recipes.module.css';

/**
 * Shown instead of a raw error whenever the household has no usable provider —
 * either because `GET /v1/providers/capabilities` said `configured: false`, or
 * because `POST /v1/recipes/suggest` answered 409 provider-not-configured.
 *
 * It used to be forty-seven lines of prose telling the reader to edit the server's
 * environment and restart the service. That was accurate when nothing in the API
 * created a provider configuration — but it made the product's headline feature
 * reachable only by whoever had a shell on the host, and, once revision `0016` added
 * the consent gate, it also asked that person to date an agreement in the
 * household's name. Consent is not something an operator may give on somebody
 * else's behalf, so the instruction was not merely inconvenient; it was asking for
 * a record that would have been false.
 *
 * So this screen now *is* the configuration, rather than an explanation of where to
 * find it: `POST /v1/providers` and the two consent routes exist, and the panel
 * below drives them. The wrapper keeps only the framing a recipe screen needs —
 * what still works without a model, and why this household's provider is refused
 * right now — and hands the rest to `features/providers`, which owns the form, the
 * consent wording and the list. Kept separable rather than inlined here because the
 * same panel belongs on the household settings screen too; mounting it there is a
 * one-line change in a file this work does not own.
 */
export function ProviderSetup({ capabilities }: { capabilities: ProviderCapabilities | null }) {
  // `configured: false` with a mode already set is a configuration that exists and
  // cannot be used — a withdrawn agreement, a key the provider refused, an Ollama
  // nobody probed. The panel below shows which, and `degraded_reasons` carries the
  // sentence the server wrote for exactly this state, so it is repeated rather than
  // paraphrased here.
  const existing = capabilities?.mode ?? null;
  const reasons = capabilities?.degraded_reasons ?? [];

  return (
    <div className={styles.setup}>
      <h2 className={styles.title}>Suggestions de recettes non configurées</h2>
      <p className={styles.lead}>
        Le reste de Chaudron fonctionne : l’inventaire, le scan et l’ajout d’articles ne dépendent
        d’aucun fournisseur d’IA. Seules les suggestions de recettes et la lecture d’un ticket
        photographié en ont besoin.
      </p>

      {existing !== null && reasons.length > 0 ? (
        <ul className={styles.setupList}>
          {reasons.map(formatDegradedReason).map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}

      <ProviderConfigPanel />
    </div>
  );
}
