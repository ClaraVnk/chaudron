import styles from './SafetyNotice.module.css';

/**
 * The three standing warnings of ADR-0009.
 *
 * None can be dismissed, and none remembers anything: there is no close button,
 * no "do not show again", no persisted flag. A warning that can be turned off is
 * read once, by the person who set the app up, and never by the grandparent
 * cooking on a Sunday. They are rendered wherever the matching information is
 * entered or displayed, every time.
 */

export function AllergenDisclaimer() {
  return (
    <div className={styles.notice} role="note">
      <p className={styles.title}>
        <span className={styles.glyph} aria-hidden="true">
          !
        </span>
        Les données allergènes ne sont pas une garantie médicale
      </p>
      <p>
        Elles viennent d’Open Food Facts — un wiki alimenté par la communauté — et de ce que vous
        saisissez ici. Un produit peut n’avoir aucune donnée, ou une donnée fausse. En cas
        d’allergie sévère, lisez l’emballage : Chaudron ne remplace pas cette lecture.
      </p>
    </div>
  );
}

/**
 * The one that has to sit *between* the two lists on the member form.
 *
 * Both lists are applied the same way — the product is removed from the
 * inventory before the model is asked anything — so nothing on screen
 * distinguishes them by behaviour. What distinguishes them is what stands behind
 * a product that says nothing: for an allergen, a manufacturer who was legally
 * obliged to declare it, so silence is a claim; for an ingredient, Open Food
 * Facts having parsed a word, so silence is silence. Without this paragraph, a
 * household that has ticked "Kiwi" and seen it work would be entitled to tick
 * "Arachides" in the same list and expect the same thing — and the two are not
 * the same thing.
 */
export function AvoidedIngredientDisclaimer() {
  return (
    <div className={styles.notice} role="note">
      <p className={styles.title}>
        <span className={styles.glyph} aria-hidden="true">
          !
        </span>
        Ces exclusions sont au mieux, pas une garantie
      </p>
      <p>
        Chaudron retire de l’inventaire les produits dont la liste d’ingrédients nomme l’aliment
        coché. Mais sur les produits français d’Open Food Facts, <strong>13 % seulement</strong>{' '}
        publient une liste exploitable : un produit qui ne dit rien est donc conservé, même s’il en
        contient. Et une liste peut être incomplète ou fausse — c’est un wiki. Pour une allergie,
        cochez la case correspondante au-dessus : c’est la liste réglementaire, un industriel a
        l’obligation d’y déclarer, et ce n’est pas la même chose.
      </p>
    </div>
  );
}

export function InfantDisclaimer() {
  return (
    <div className={styles.notice} role="note">
      <p className={styles.title}>
        <span className={styles.glyph} aria-hidden="true">
          !
        </span>
        Mode nourrisson : ces règles ne remplacent pas un pédiatre
      </p>
      <p>
        La diversification alimentaire se discute avec un professionnel de santé. Chaudron applique
        une table d’aliments déconseillés par tranche d’âge ; elle ne connaît ni votre enfant, ni
        son histoire médicale.
      </p>
    </div>
  );
}
