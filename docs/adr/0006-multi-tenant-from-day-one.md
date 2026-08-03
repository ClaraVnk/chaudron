# 0006. Multi-tenant dès le premier jour

## Statut

Accepté — 2026-08-03

## Contexte

En phase 1, Chaudron sert un seul foyer. La modélisation la plus économique serait donc mono-tenant : des tables `item`, `stock_entry`, `shopping_list_item` sans notion de propriétaire, et une authentification réduite à un utilisateur unique.

La phase 2 envisagée est une ouverture publique multi-utilisateurs. Le stock, la liste de courses et l'historique d'achats appartiennent à un **foyer**, pas à une personne : deux conjoints partagent le même frigo et doivent voir le même stock. L'unité d'isolation naturelle est donc le foyer (`household`), pas l'utilisateur.

La question n'est pas de savoir si le multi-tenant sera nécessaire, mais quand le payer. Or ce coût n'est pas linéaire dans le temps : ajouter une colonne de tenant à un schéma existant est mécanique, mais **rétrofitter le filtrage de tenant dans un code applicatif qui n'en a jamais eu ne l'est pas**. Chaque requête écrite sans clause de tenant est un chemin de fuite potentiel, et il faut toutes les auditer une par une, sans qu'aucun test existant n'échoue si on en oublie une.

## Décision

Le multi-tenant est présent dès la première migration, même si une seule ligne existe dans `household`.

**Modèle.** Une table `household` est la racine d'isolation. Toute table métier porte une colonne `household_id` non nulle, avec clé étrangère vers `household`. Les contraintes d'unicité fonctionnelles sont composites et incluent toujours `household_id` (par exemple `UNIQUE (household_id, barcode)` et non `UNIQUE (barcode)`). Les index de lecture sont préfixés par `household_id`. Le lien utilisateur ↔ foyer passe par une table d'association `household_member` portant un rôle, ce qui permet à une personne d'appartenir à plusieurs foyers sans changer le modèle.

**Accès aux données.** Aucune requête métier ne s'exécute sans filtre de tenant. Le `household_id` courant est résolu une seule fois, à la frontière HTTP, à partir du contexte d'authentification — **jamais lu depuis le corps ou les paramètres de la requête**. Il est propagé explicitement par la couche applicative jusqu'au dépôt. Les fonctions de dépôt prennent le `household_id` en paramètre obligatoire, de sorte que l'omettre soit une erreur de typage détectée par `mypy`, pas un défaut d'exécution.

**Tests.** Toute ressource exposée dispose d'un test d'isolation : deux foyers sont créés avec des données propres, et les opérations du foyer A sur les identifiants du foyer B doivent renvoyer `404` (jamais `403`, qui confirmerait l'existence de la ressource). Ces tests sont obligatoires pour toute nouvelle ressource ; leur absence est un motif de refus en revue.

**Isolation stricte.** Un identifiant appartenant à un autre foyer se comporte comme un identifiant inexistant. Cela vaut pour les lectures comme pour les écritures.

## Conséquences

### Positives

- La phase 2 ne nécessite ni migration de données risquée, ni audit exhaustif du code d'accès.
- Le partage entre membres d'un même foyer — nécessaire dès la phase 1 pour un couple — est acquis sans travail supplémentaire.
- Les tests d'isolation constituent un filet permanent : une régression d'étanchéité fait échouer la CI au lieu de fuiter en production.
- Les index préfixés par `household_id` sont ceux dont on a besoin de toute façon, puisque toutes les lectures sont scopées.
- Le modèle supporte des scénarios ultérieurs (résidence secondaire, colocation, foyer partagé temporairement) sans refonte.

### Négatives

- **Toutes les signatures sont plus lourdes.** Chaque fonction de dépôt porte un paramètre supplémentaire, chaque requête une clause de plus. En phase 1 mono-foyer, c'est de la cérémonie pure : la valeur est toujours la même.
- Les fixtures de test sont plus verbeuses : créer un foyer avant de créer un article, dans chaque scénario.
- La discipline dépend d'une convention. Le typage aide, mais un `session.execute(select(Item))` sans filtre reste écrivable et compile. Une fuite reste possible tant que le filtrage n'est pas appliqué par la base elle-même.
- Le coût est payé immédiatement, le bénéfice ne se matérialise qu'à la phase 2 — qui pourrait ne jamais arriver. C'est un pari assumé, pas une certitude.
- Certaines requêtes analytiques transverses (statistiques globales, référentiel produit mutualisé) devront explicitement sortir du scope de tenant, ce qui crée une exception à documenter et à protéger séparément.

## Alternatives écartées

- **Mono-tenant maintenant, migration à la phase 2** — le choix par défaut. Écarté : la migration se décompose en ajout de colonne (facile, `ALTER TABLE ... ADD COLUMN household_id`, backfill à une valeur unique), reprise de toutes les contraintes d'unicité (moyennement risqué : passer de `UNIQUE (barcode)` à `UNIQUE (household_id, barcode)` sous charge), puis **audit de chaque requête applicative** (le vrai coût). Cette dernière étape n'a pas de garde-fou : rien n'échoue si on oublie une requête, la fuite se découvre en production, chez un utilisateur, sur des données personnelles. Sur une base de quelques dizaines de fichiers d'accès aux données, c'est plusieurs jours de travail à haut risque, contre quelques heures de discipline étalées maintenant.
- **Une base ou un schéma PostgreSQL par foyer** — isolation la plus forte, imposée par le moteur. Écarté : les migrations doivent alors être appliquées à N schémas, le pooling de connexions se complique, et l'exploitation d'un VPS unique devient disproportionnée pour la taille de la donnée. Reste l'option de référence si un besoin de conformité stricte apparaît.
- **Row-Level Security PostgreSQL** — l'isolation est appliquée par la base, pas par la convention applicative, ce qui supprime la principale faiblesse de la décision retenue. Écarté **pour maintenant** : RLS exige de propager le tenant dans la session (`SET LOCAL`), ce qui interagit mal avec le pooling de connexions et demande une gestion soigneuse en contexte asynchrone. C'est le renforcement naturel, pas une alternative concurrente : le schéma retenu (`household_id` partout) est exactement le prérequis de RLS.
- **Tenant identifié par sous-domaine ou en-tête HTTP** — pratique en SaaS B2B. Écarté : le tenant doit dériver de l'authentification seule. Toute source contrôlable par le client est une élévation de privilège offerte.

## Révision

Activer Row-Level Security sur les tables métier avant l'ouverture publique de la phase 2 : le schéma est déjà compatible, et cela déplace la garantie d'isolation de la convention vers le moteur.

Passer à une isolation par schéma si un besoin de conformité impose une séparation physique des données, ou si un foyer unique atteint un volume qui justifie un partitionnement.
