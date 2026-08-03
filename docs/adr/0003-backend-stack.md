# 0003. Stack backend : FastAPI, SQLAlchemy 2.x, PostgreSQL, uv

## Statut

Accepté — 2026-08-03

## Contexte

Chaudron expose une API consommée par une PWA React séparée (cf. ADR-0004), et doit traiter des charges hétérogènes : requêtes CRUD courtes sur le stock, appels sortants lents et imprévisibles vers des modèles de langage (cf. ADR-0005), et appels à Open Food Facts pour la résolution EAN. Ces appels sortants dominent le temps de réponse et sont majoritairement de l'attente réseau.

Le développeur est seul et connaît Python. Le déploiement visé est un VPS Rocky Linux 10 en SELinux Enforcing, via quadlets systemd et conteneurs Podman.

Le modèle de données est multi-tenant dès le départ (cf. ADR-0006), avec un `household_id` porté par toutes les tables métier — ce qui exige un ORM capable d'exprimer proprement des contraintes composites et des index partiels.

## Décision

**Runtime** : Python 3.14, la dernière version stable, pinnée dans `pyproject.toml` (`requires-python = ">=3.14"`) et `.python-version`.

**Framework HTTP** : FastAPI. ASGI natif, donc I/O concurrente sans thread pool sur les appels sortants ; validation d'entrée par Pydantic aux frontières ; OpenAPI généré à partir des types, ce qui donne un contrat exploitable côté frontend sans documentation à maintenir à la main.

**Persistance** : SQLAlchemy 2.x en style déclaratif typé (`Mapped[...]`, `mapped_column`), avec `asyncpg` comme driver. Migrations par Alembic, chaque migration fournissant `upgrade` et `downgrade`.

**Base de données** : PostgreSQL 16, en conteneur dédié, volume nommé, mot de passe en secret Podman, volume monté avec le suffixe `:Z` sous SELinux.

**Outillage** : `uv` pour la toolchain et les dépendances (`uv.lock` versionné) ; `ruff` pour le lint et le format ; `mypy` en mode strict ; `pytest` avec `pytest-cov`.

**Tests** : PostgreSQL est le seul moteur exercé, en local comme en CI. La CI démarre un service `postgres:16` dédié. Il n'y a pas de mode SQLite, même pour les tests unitaires.

## Conséquences

### Positives

- Une seule chaîne de types, du corps de requête HTTP jusqu'aux colonnes : Pydantic aux frontières, `Mapped[...]` en base, `mypy --strict` entre les deux.
- L'ASGI absorbe la latence des appels LLM sans multiplier les workers : un handler bloqué sur une réponse de modèle ne bloque pas les requêtes de stock.
- PostgreSQL apporte les types dont le domaine a besoin : `timestamptz` (dates de péremption avec fuseau), `numeric` (quantités), `jsonb` indexable (payload brut d'un ticket parsé), contraintes d'unicité composites sur `(household_id, ...)`.
- `uv.lock` versionné rend les builds reproductibles ; `uv sync --frozen` en CI garantit que le conteneur de production installe exactement ce qui a été testé.
- Tester contre le moteur de production supprime toute une classe de bugs qui n'apparaissent qu'au déploiement.

### Négatives

- **Le code asynchrone se propage.** Une fonction `async` ne s'appelle proprement que depuis un contexte `async` : une bibliothèque synchrone bien choisie devra être encapsulée dans un thread, ou remplacée. C'est un coût structurel, pas une gêne ponctuelle.
- Les sessions SQLAlchemy asynchrones sont un piège à N+1 silencieux : le lazy loading lève une exception en contexte async, ce qui force à écrire tous les `selectinload` explicitement. C'est meilleur pour la performance, plus verbeux à écrire.
- La boucle de test est plus lourde : chaque exécution suppose un PostgreSQL joignable. Pas de `pytest` sur une machine nue sans conteneur en marche.
- `mypy --strict` sur du code SQLAlchemy async coûte des annotations et des `cast()` ponctuels ; la friction est réelle sur les requêtes complexes.
- Python 3.14 est récent : certaines dépendances peuvent ne pas fournir de roue précompilée, ce qui allonge les builds ou impose une compilation.
- Deux codebases (backend, frontend) signifient deux pipelines, deux jeux de dépendances, et un contrat OpenAPI à garder synchronisé.

## Alternatives écartées

- **Django + Django REST Framework** — admin fourni, ORM mature, écosystème complet, et l'auth multi-tenant de la phase 2 y serait plus vite câblée. Écarté pour deux raisons : le support async de Django reste partiel là où il compte (l'ORM, précisément le chemin qu'emprunteraient les handlers qui attendent un LLM), et le monolithe encourage à mettre la logique métier dans les vues, ce qui est exactement la frontière que l'on veut tenir. On sacrifie un vrai confort — l'admin Django aurait couvert gratuitement une partie du back-office.
- **Litestar** — plus rapide que FastAPI sur les benchmarks, DI plus propre. Écarté : écosystème et corpus de réponses nettement plus petits, pour un gain qui ne se manifeste pas sur une charge dominée par l'attente réseau.
- **Flask + SQLAlchemy** — familier et minimal. Écarté : WSGI synchrone, validation à câbler à la main, OpenAPI à maintenir séparément.
- **Go ou Node** — Go pour la robustesse du déploiement, Node pour partager le langage avec le frontend. Écartés : l'écosystème Python des modèles multimodaux et du traitement d'image est nettement plus riche, et c'est le langage que le développeur maîtrise le mieux.
- **SQLite** — zéro opération, fichier unique, séduisant pour un usage familial. Écarté explicitement, c'est la décision la plus importante de cet ADR :
  - Les écritures sont sérialisées à l'échelle de la base. Un job de fond (parsing de ticket, appel LLM, import d'e-mail) bloque une requête utilisateur.
  - Il n'y a pas de vrai typage : booléens en `0`/`1`, horodatages naïfs sans fuseau, JSON stocké en texte. Une date de péremption sans fuseau est un bug qui se découvre au changement d'heure.
  - La sauvegarde est une copie de fichier, pas une archive restaurable table par table.
  - Migrer PostgreSQL → SQLite tardivement se ferait à travers les modèles ORM, pas par un dump réécrit, les deux moteurs ne s'accordant sur presque aucun type.
- **SQLite pour les tests uniquement, PostgreSQL en production** — le compromis courant. Écarté : le moteur de production n'est alors jamais testé. Les divergences (types, contraintes différées, `jsonb`, index partiels, comportement transactionnel) apparaissent en production, là où elles coûtent le plus cher.

## Révision

Réévaluer PostgreSQL 16 vers une version majeure supérieure une fois celle-ci en support long terme et disponible en image officielle stable. Réévaluer le choix de FastAPI si le projet adopte un frontend rendu côté serveur, auquel cas la séparation des codebases perd sa justification.
