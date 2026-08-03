# 0002. Pas d'intégration aux comptes drive des enseignes

## Statut

Accepté — 2026-08-03

## Contexte

Le cas d'usage le plus évident de Chaudron est le remplissage automatique du stock après une commande drive : l'utilisateur commande chez Courses U, Intermarché ou Chronodrive, et son stock se met à jour sans saisie. C'est la fonctionnalité que tout utilisateur demandera en premier.

Aucune de ces enseignes ne publie d'API partenaire accessible à un développeur individuel. Les seuls chemins techniques sont :

1. **Scraping authentifié** : l'utilisateur confie ses identifiants enseigne à Chaudron, qui se connecte à sa place et récupère l'historique de commandes.
2. **Automatisation de navigateur** (Playwright headless) côté serveur, variante du précédent avec les mêmes prérequis de credentials.
3. **Reverse-engineering des API mobiles** des applications enseigne.

Les trois partagent les mêmes propriétés : ils exigent de stocker des identifiants réutilisables donnant accès à un compte marchand (moyens de paiement enregistrés, adresse, historique d'achats), ils violent les CGU de chaque enseigne, et ils cassent au premier changement de front-end — sans préavis, sans page de statut, sans version pinnable. Multiplié par le nombre d'enseignes françaises, c'est une charge de maintenance permanente et non planifiable.

En phase 1 (usage familial), le risque est contenu. En phase 2 (ouverture publique), Chaudron deviendrait un dépôt centralisé de credentials de comptes marchands : une cible dont la valeur pour un attaquant dépasse largement celle de la donnée métier de l'application.

## Décision

Chaudron n'intègre aucun compte drive d'enseigne. Aucune fonctionnalité ne demande, ne stocke ni ne transmet d'identifiant de compte marchand.

L'alimentation du stock repose sur quatre voies, toutes initiées par l'utilisateur :

1. **Saisie manuelle** — le socle, toujours disponible.
2. **Scan de code-barres** — résolution EAN via Open Food Facts (API publique, licence ODbL, pas d'authentification).
3. **Photo de ticket de caisse** — parsée par un modèle multimodal (cf. ADR-0005). Fonctionne pour les achats en magasin comme pour le drive (ticket remis au retrait).
4. **Capture d'e-mail de confirmation transférée** — l'utilisateur transfère son e-mail de confirmation de commande à une adresse dédiée par foyer (`<household_token>@inbox.<domain>`). Le contenu est parsé pour en extraire les lignes de commande.

La voie 4 obtient une large part du bénéfice de l'intégration drive sans aucun de ses prérequis : c'est l'utilisateur qui pousse la donnée, il n'y a rien à authentifier chez l'enseigne, et l'e-mail de confirmation est un format bien plus stable qu'un DOM de site marchand. Le transfert reste un geste manuel — mais un geste par commande, pas par article.

## Conséquences

### Positives

- Aucun credential de compte marchand dans la base : la surface d'attaque la plus coûteuse du produit n'existe pas.
- Aucune dépendance à des surfaces non contractuelles : pas de casse silencieuse au prochain redesign d'une enseigne.
- Pas de risque juridique lié à la violation de CGU, ni de blocage de compte utilisateur pour usage automatisé.
- Le périmètre ne croît pas avec le nombre d'enseignes : ajouter une enseigne coûte au plus un parseur d'e-mail, jamais un pipeline d'authentification.
- La phase 2 reste ouverte : pas de dette de sécurité à purger avant d'ouvrir au public.

### Négatives

- **On perd la fonctionnalité la plus attendue.** Un utilisateur qui compare Chaudron à un concurrent intégré au drive verra un produit en retrait, et l'argument « c'est plus sûr » ne compense pas en démonstration.
- Le transfert d'e-mail est un geste manuel à chaque commande. Une friction faible mais réelle, et le taux d'oubli sera élevé.
- L'OCR de ticket et le parsing d'e-mail sont **approximatifs par nature** : libellés enseigne tronqués ou abrégés (`PAT SABL BEURRE 250G`), absence de code EAN sur le ticket, quantités implicites. Le rapprochement avec un référentiel produit exigera une étape de correction manuelle, elle-même une friction.
- Chaque format d'e-mail de confirmation est un parseur à écrire et à maintenir. La charge est plus faible qu'un scraper, elle n'est pas nulle.
- La couverture des produits frais et de la vente en vrac restera médiocre quelle que soit la voie retenue (pas d'EAN, pesée au ticket).
- Recevoir des e-mails utilisateurs crée sa propre surface : la boîte de réception est un point d'entrée non authentifié qu'il faut traiter comme une donnée hostile (validation stricte de l'expéditeur, quotas, pas de désérialisation naïve des pièces jointes).

## Alternatives écartées

- **Scraping avec credentials stockés chiffrés** — techniquement faisable, chiffrement au repos disponible. Écarté : le chiffrement au repos ne protège pas d'une compromission applicative, puisque l'application doit pouvoir déchiffrer pour s'authentifier. Le risque de fuite reste entier ; seule l'absence de secret l'élimine.
- **Extension navigateur côté client** — le credential ne quitte pas la machine de l'utilisateur, ce qui répond à l'objection sécurité. Écarté : c'est une seconde codebase à maintenir, avec ses propres cycles de revue par les stores d'extensions, pour un produit dont le socle est une PWA mobile (cf. ADR-0004) où les extensions n'existent pas.
- **Attendre une API partenaire officielle** — le chemin propre. Écarté : aucune enseigne française n'en propose à un développeur individuel, et rien n'indique que cela change. Ce n'est pas une alternative, c'est un report indéfini.
- **Agrégateur tiers** (service commercial exposant les historiques d'achat) — externaliserait le problème. Écarté : aucun acteur crédible sur le marché français de la grande distribution alimentaire, et cela reviendrait à déplacer le stockage des credentials chez un tiers sans en réduire le risque pour l'utilisateur.

## Révision

Rouvrir la décision si l'un de ces signaux apparaît :

- Une enseigne publie une **API partenaire documentée avec OAuth** (délégation par jeton révocable, sans partage d'identifiant). C'est le seul changement qui invalide le raisonnement de fond.
- Un standard interprofessionnel d'export d'historique d'achat émerge (typiquement dans le sillage du droit à la portabilité RGPD, article 20).
- Les mesures d'usage montrent que le transfert d'e-mail est utilisé pour moins de 20 % des commandes malgré une UX aboutie : le compromis ne tient alors plus, et il faudra soit accepter la saisie manuelle comme voie principale, soit reconsidérer l'extension navigateur.
