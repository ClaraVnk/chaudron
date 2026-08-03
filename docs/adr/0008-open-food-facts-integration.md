# 0008. Stratégie d'intégration d'Open Food Facts

## Statut

Accepté — 2026-08-03.

## Contexte

La résolution d'un code EAN vers une fiche produit s'appuie sur Open Food Facts,
service communautaire gratuit sous licence ODbL. L'étude de faisabilité
(`docs/technical-notes-scanning.md`) a mis au jour quatre faits qui changent la
nature de cette intégration.

**Le plafond de débit s'applique par adresse IP, pas par utilisateur.** La
documentation d'Open Food Facts précise que la limite s'applique par utilisateur
*lorsque les requêtes proviennent directement des clients*. En centralisant les
appels dans le backend — ce que nous faisons, et qui reste le bon choix — toutes
les requêtes sortent d'une seule IP : le plafond devient global à l'instance
entière. L'ordre de grandeur relevé est de quinze requêtes par minute, avec
bannissement d'IP au dépassement. Le comportement en dépassement a été observé
en pratique : l'API répond en HTML, pas en JSON.

**Le cache ne peut donc pas être scopé par foyer.** L'ADR-0006 impose un
`household_id` sur toute table métier. Appliqué mécaniquement au cache produit,
il multiplierait les appels sortants par le nombre de foyers, pour un contenu
strictement identique — exactement ce que le plafond interdit.

**Une part substantielle des articles d'un placard réel n'a pas de code
exploitable.** Produits absents du référentiel, emballages froissés, fruits,
légumes, boucherie, vrac. Et surtout les **codes internes magasin** à préfixe
`02` et `20`–`29` : utilisés pour les articles à poids variable, ils embarquent
le prix, changent donc à chaque achat, et ne figureront jamais dans un
référentiel public.

**La v2 de l'API est dépréciée.** La v3 est la version courante et son contrat
d'erreur diffère : `result.id == "product_not_found"` avec un HTTP 404, là où la
v2 renvoyait `status: 0`.

## Décision

**Le catalogue produit est un référentiel externe partagé, pas une donnée de
foyer.** Il est matérialisé par `product` avec `household_id IS NULL`. Les fiches
créées ou corrigées par un foyer portent un `household_id` non nul et sont
isolées. C'est une exception explicite et bornée à la règle de l'ADR-0006, et la
seule.

**Le cache est une condition de fonctionnement, pas une optimisation.** Toute
résolution passe par le cache d'abord. Les échecs sont mis en cache négatif : un
code absent d'Open Food Facts ne doit pas déclencher un appel à chaque scan.

**Les codes internes magasin sont détectés côté client** à partir de leur
préfixe, et ne donnent lieu à aucun appel réseau — ni au backend, ni à Open Food
Facts. L'utilisateur passe directement à la saisie manuelle.

**La saisie manuelle est une fonctionnalité de premier rang**, pas un repli
dégradé. Une fiche éditée localement l'emporte sur tout rafraîchissement
ultérieur venant d'Open Food Facts : le champ qui porte cette précédence est
prévu dès la première migration, parce que l'ajouter après coup exige de
retrouver quelles fiches avaient été corrigées à la main.

**Le développement se fait contre l'environnement de recette**
(`world.openfoodfacts.net`), comme la documentation le demande, et l'API v3 est
la cible.

**Un identifiant d'appelant honnête est envoyé** dans l'en-tête `User-Agent`,
conformément à la politique du projet — nom de l'application, version, adresse de
contact.

**En phase 2, l'import du dump local devient un prérequis.** Servir des
utilisateurs externes derrière un plafond de quinze requêtes par minute n'est pas
tenable. L'API ne sert alors plus qu'à combler les manques ponctuels du dump.

## Conséquences

### Positives

- L'application continue de résoudre les produits déjà vus quand Open Food Facts
  est indisponible ou nous a bannis.
- Le cache mutualisé rend le coût d'appel indépendant du nombre de foyers.
- Détecter les codes magasin côté client économise un aller-retour complet sur
  des articles qui ne seront jamais résolus.
- Aucun coût de licence : l'alternative qualitativement supérieure, CodeOnline
  Food de GS1 France, exige une adhésion à cinq chiffres.

### Négatives

- **Le plafond reste une limite dure en phase 1.** Un foyer qui déballe ses
  courses peut scanner plus vite que le débit autorisé. Il faut une file d'attente
  côté serveur, une limitation de débit propre et un message honnête à
  l'utilisateur — pas une erreur brute.
- **L'exception à l'ADR-0006 est une brèche dans une règle qui vaut par son
  caractère absolu.** « `household_id` partout, sauf ici » est plus difficile à
  faire respecter que « `household_id` partout ». Le risque est qu'un autre
  développeur invoque ce précédent pour une table qui, elle, contient bien des
  données de foyer.
- **L'ODbL impose un partage à l'identique.** Tant que le référentiel n'est que
  consulté, l'obligation reste théorique ; dès qu'un dump est importé et enrichi,
  elle devient réelle. Elle devra être instruite avant la phase 2, pas pendant.
- **L'import du dump a un coût d'infrastructure** — espace disque, rafraîchissement
  périodique, fenêtre de reconstruction — qui n'existe pas aujourd'hui.
- **La couverture n'est pas la complétude.** Environ 1,25 million de produits
  vendus en France sont présents, mais le taux de complétude des champs utiles
  n'a pas pu être mesuré, le plafond de débit ayant empêché la mesure. À vérifier
  sur un dump local avant de promettre quoi que ce soit dans l'interface.

## Alternatives écartées

- **Appels effectués depuis le navigateur du client**, ce qui ramènerait le
  plafond à un compte par utilisateur. Écarté : expose la stratégie d'appel,
  empêche toute mise en cache partagée, et rend l'application dépendante de la
  disponibilité d'Open Food Facts depuis le réseau de chaque utilisateur. Le gain
  ne compense pas la perte du cache mutualisé.
- **Cache scopé par foyer**, cohérent avec l'ADR-0006 sans exception. Écarté : il
  multiplie les appels sortants par le nombre de foyers pour un contenu
  identique, ce que le plafond interdit.
- **CodeOnline Food (GS1 France)**, données fournies par les marques et de
  meilleure qualité. Écarté : adhésion GS1 à cinq chiffres, hors de portée d'un
  projet solo.
- **Import du dump dès la phase 1.** Écarté : coût d'infrastructure immédiat pour
  un foyer unique dont le volume de scans tient largement sous le plafond.

## Révision

- Importer le dump dès qu'une deuxième instance ou un deuxième foyer actif
  existe, sans attendre l'ouverture publique — le plafond est global, donc il se
  partage.
- Réexaminer l'exception à l'ADR-0006 si une seconde table réclame le même
  traitement : deux exceptions ne font plus une exception, elles font une règle
  mal formulée.
- Instruire les obligations ODbL avant tout import de dump.
