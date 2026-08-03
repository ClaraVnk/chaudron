# 0001. Consigner les décisions d'architecture

## Statut

Accepté — 2026-08-03

## Contexte

Chaudron est développé en solo. Les décisions structurantes (choix de stack, périmètre fonctionnel, modèle de données multi-tenant) se prennent en quelques minutes et s'oublient en quelques semaines. Six mois plus tard, la question n'est plus « qu'est-ce qu'on a choisi » — le code le dit — mais « pourquoi », et surtout « qu'est-ce qu'on avait écarté, et pour quelle raison ». Sans trace, chaque relecture rejoue le même arbitrage à partir de zéro, souvent avec moins de contexte qu'à l'origine.

Le risque est amplifié par la phase 2 envisagée (ouverture publique multi-utilisateurs) : des décisions prises pour un usage familial devront être réévaluées, et il faut pouvoir distinguer ce qui était un compromis assumé de ce qui était une contrainte réelle.

Le format ADR de Michael Nygard est court, versionné avec le code, et lisible sans outillage.

## Décision

Toute décision d'architecture significative est consignée dans un fichier `docs/adr/NNNN-titre-en-kebab-case.md`, numéroté séquentiellement, versionné avec le code, rédigé en français avec tous les identifiants techniques en anglais.

Structure imposée : `# NNNN. Titre`, puis `## Statut` (Accepté / Rejeté / Remplacé par ADR-NNNN, avec la date), `## Contexte`, `## Décision`, `## Conséquences` (positives et négatives séparées), `## Alternatives écartées` (une raison par alternative), et `## Révision` quand un signal concret peut rouvrir la décision.

Un ADR est écrit quand la décision : engage une dépendance externe difficile à retirer, contraint le modèle de données, définit une frontière entre couches ou services, exclut une fonctionnalité que quelqu'un pourrait raisonnablement attendre, ou expose à un coût récurrent (financier, opérationnel, de maintenance).

Un ADR n'est **pas** écrit pour : un choix de bibliothèque utilitaire remplaçable en une heure, une convention de nommage, un détail d'implémentation local à un module.

Les ADR sont **immuables**. On ne modifie pas un ADR accepté : on en écrit un nouveau qui le remplace, et on marque l'ancien `Remplacé par ADR-NNNN`. L'historique des décisions abandonnées a autant de valeur que celui des décisions actives.

## Conséquences

### Positives

- Le contexte d'une décision survit à l'oubli et au changement de mainteneur.
- Les alternatives écartées sont documentées : on ne repropose pas une piste déjà instruite sans nouvel argument.
- La section `Révision` transforme un choix figé en choix conditionnel : on sait à quel signal le rouvrir.
- Un ADR force à formuler les conséquences négatives, ce qui révèle parfois qu'une décision n'est pas mûre.

### Négatives

- Rédiger un ADR correct coûte 30 à 60 minutes. Sur un projet solo, c'est du temps pris sur l'implémentation.
- Le format invite à la rationalisation *a posteriori* : on justifie un choix déjà fait plutôt que d'instruire l'alternative. Le seul garde-fou est l'honnêteté sur les conséquences négatives.
- Des ADR non maintenus (décision changée dans le code, ADR jamais remplacé) sont pires que pas d'ADR : ils décrivent avec autorité un système qui n'existe plus.
- Le seuil « décision significative » reste subjectif. On écrira des ADR inutiles et on en oubliera d'utiles.

## Alternatives écartées

- **Aucune documentation de décision** — le mode par défaut sur un projet solo. Écarté : la phase 2 implique de rouvrir des décisions prises en phase 1, sans mémoire fiable du raisonnement d'origine.
- **Commentaires dans le code** — proches du code mais limités à un fichier. Écarté : une décision d'architecture porte par définition sur plusieurs modules ou sur une absence de code (cf. ADR-0002), qu'aucun commentaire ne peut héberger.
- **Wiki ou notes externes (Notion, Obsidian)** — plus confortables à rédiger. Écarté : la documentation se désynchronise du code dès qu'elle vit ailleurs, et n'apparaît pas dans les diffs de revue.
- **Messages de commit détaillés** — versionnés et datés. Écarté : illisibles en tant que corpus, et un `git log` ne se parcourt pas pour répondre à « pourquoi PostgreSQL ».
- **MADR ou un format plus riche** (tableaux de critères pondérés) — plus rigoureux sur les décisions à plusieurs parties prenantes. Écarté : surdimensionné pour un décideur unique, le coût de rédaction ferait abandonner la pratique.

## Révision

Si le projet accueille un second contributeur régulier, réévaluer le format : un template plus structuré (MADR) et une revue des ADR en pull request deviennent alors justifiables.
