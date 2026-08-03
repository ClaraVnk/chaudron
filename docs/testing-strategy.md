# Stratégie de test

Document de cadrage. Décrit ce qu'on teste, à quel niveau, et surtout ce qu'on ne
teste pas. Les décisions structurantes qui le contraignent sont dans les ADR :
[0003](adr/0003-backend-stack.md) (PostgreSQL partout, jamais SQLite),
[0005](adr/0005-llm-provider-abstraction.md) (la suite de conformité d'adaptateur
est la condition de la décision) et [0006](adr/0006-multi-tenant-from-day-one.md)
(tests d'isolation obligatoires par ressource).

Le mode d'emploi — commandes, prérequis Podman, ajout d'un adaptateur au harnais —
est dans [`backend/tests/README.md`](../backend/tests/README.md), en anglais comme
tout ce qui est destiné à un contributeur.

Tous les identifiants techniques cités sont en anglais, conformément à la convention
du projet.

---

## 1. État actuel et objectif du document

Le projet est en cadrage : documentation, ADR, schéma de données. **Il n'y a pas de
code de fonctionnalité**, donc pas de fonctionnalité à tester. Ce document et le
harnais qui l'accompagne existent pour que le premier code écrit arrive dans un
environnement de test déjà prêt — c'est le seul moment où la stratégie de test coûte
peu, parce qu'elle ne demande de rattraper aucun existant.

Ce qui est déjà en place et exécutable :

- les fixtures de base de données (PostgreSQL éphémère via Podman, session
  transactionnelle annulée, fabriques de foyers et d'utilisateurs) ;
- les gardes d'isolation multi-tenant au niveau du schéma, qui passent aujourd'hui
  sur les 17 tables déclarées ;
- le harnais de conformité d'adaptateur LLM, paramétré sur les cinq fournisseurs,
  entièrement en `skip` tant qu'aucun adaptateur n'existe.

Ce qui est en `skip` l'est avec une raison lisible, jamais avec un `xfail`
silencieux. **Un test qui échoue est un signal ; un test qui passe sans rien
vérifier est un mensonge ; un `skip` motivé est un élément de backlog.** Les trois se
lisent dans la sortie de `pytest -ra`, et c'est la seule raison pour laquelle des
`skip` sont acceptables ici.

---

## 2. La pyramide retenue

Pas de proportions dogmatiques : la forme découle de la nature du système. Chaudron est
une application dont l'essentiel du risque est concentré sur trois points — les
règles de quantités, l'étanchéité entre foyers, et le comportement face à des
fournisseurs de modèles hétérogènes. C'est là que porte l'effort.

| Niveau | Ce qu'on y teste | Coût | Volume attendu |
|---|---|---|---|
| **Domaine** (pur) | Conversions d'unités, allocation FEFO, fusion de lots, calcul de péremption, règles de disponibilité d'ingrédients | µs, aucune I/O | Le plus gros contingent |
| **Schéma** (métadonnées) | Présence du tenant, contraintes composites, unicité scopée | ms, aucune I/O | Une poignée, mais paramétrés sur **toutes** les tables |
| **Services + dépôts** (PostgreSQL réel) | Cas d'usage complets, transactions, requêtes scopées, migrations | ~100 ms | Un par cas d'usage, plus les cas d'erreur |
| **Contrat d'adaptateur** (doublures) | Les cinq adaptateurs LLM face au même contrat | ms | 1 suite × 5 fournisseurs |
| **API** (ASGI in-process) | Codes de statut, validation d'entrée, autorisation, sérialisation | ~100 ms | Un chemin nominal et les refus par ressource |
| **Bout en bout** (navigateur) | Le scan de code-barres et la revue de ticket, rien d'autre | secondes | Une poignée, jamais plus |

### Ce qu'on ne teste pas

Aussi important que le reste, et volontairement explicite pour que personne n'ait à
le redécider en revue :

- **FastAPI, SQLAlchemy, Pydantic.** On ne teste pas que la validation Pydantic
  rejette un entier là où une chaîne est attendue, ni que SQLAlchemy sait faire un
  `INSERT`. Tester une bibliothèque tierce produit des tests qui cassent à chaque
  montée de version sans avoir jamais rien trouvé.
- **Les getters, les DTO, les schémas sans logique.** Leur couverture est acquise
  gratuitement par les tests des couches qui les utilisent.
- **Le rendu HTML/CSS.** Les tests de bout en bout vérifient qu'un parcours
  fonctionne, pas qu'un bouton est bleu.
- **La qualité intrinsèque des sorties de modèle en CI de PR.** « La recette
  proposée est-elle bonne » n'est ni déterministe ni gratuit : voir §8.
- **Les SDK des fournisseurs.** On teste notre traduction de leurs erreurs, jamais
  leur comportement. Ce qui *doit* être vérifié contre les vrais fournisseurs est
  cadré en §5.4.
- **Le pipeline de décodage EAN dans le navigateur.** C'est une bibliothèque tierce
  alimentée par une caméra ; on teste ce qu'on fait de la chaîne de 13 caractères
  qu'elle produit.
- **Les combinaisons pour la combinaison.** Cinq adaptateurs × chaque fonctionnalité
  × chaque capacité est une matrice explosive (ADR-0005 le dit : c'est le vrai prix
  de la décision). On la couvre par un *contrat unique paramétré*, pas par
  N suites copiées-collées.

### Ce qui n'existe pas ici

**Pas de tests unitaires sur doublure de base de données.** Il n'y a pas de mode
SQLite, pas de dépôt en mémoire qui « fait comme si ». Une requête qui n'a pas été
exécutée par PostgreSQL n'a pas été testée : les index partiels, les contraintes
différées, `jsonb`, `numeric` et le comportement transactionnel sont précisément ce
qui casse au déploiement (ADR-0003). Le test qui garantit cette règle est
`tests/test_database_harness.py` : il vérifie le dialecte et la version du moteur, et
échoue si un moteur de substitution réapparaît un vendredi soir.

---

## 3. Tester chaque couche sans trahir la règle de dépendance

La règle `api → services → domain ← infra` n'est pas une figure de style : c'est ce
qui rend le domaine testable sans base ni réseau. Elle se vérifie, elle ne se
suppose pas.

### 3.1 Le domaine se teste sans rien

Un test de domaine n'a le droit de demander **aucune fixture**. Pas de `db_session`,
pas de client HTTP, pas d'horloge système. Concrètement :

- Les entités et les règles sont des fonctions et des objets purs ; les dépendances
  externes sont des *ports* (interfaces) déclarés par le domaine et passés en
  paramètre.
- Le temps est un port comme un autre. Une règle de péremption qui appelle
  `datetime.now()` n'est pas testable : elle donne un résultat différent demain. On
  passe l'instant de référence, et le lint interdit déjà les datetimes naïves
  (`DTZ`).
- Les doublures sont des implémentations en mémoire écrites à la main
  (`FakeRecipeSuggester`, `FakeClock`), pas des mocks à assertions d'appel. Un mock
  qui vérifie « la méthode a été appelée avec ces arguments » teste l'implémentation
  courante, et casse au premier refactor qui ne change rien pour l'utilisateur.

**Comment on s'en assure concrètement** — trois filets, du plus faible au plus fort :

1. `ban-relative-imports` et `known-first-party` sont déjà configurés dans `ruff` ;
2. un test d'architecture (à écrire dès que `domain/` contient de la logique) qui
   parcourt les modules de `chaudron.domain` et échoue si l'un d'eux importe
   `sqlalchemy`, `fastapi`, `httpx` ou un SDK de fournisseur. C'est dix lignes
   d'`ast`, et c'est le seul moyen de rendre la règle exécutoire plutôt que
   déclarative ;
3. la lenteur elle-même : `pytest -m "not integration"` doit rester sous la seconde.
   Le jour où ça dérive, c'est qu'une dépendance a traversé une frontière.

### 3.2 Les services se testent contre une vraie base

Un service orchestre : il ouvre une transaction, appelle des dépôts, applique une
règle de domaine, écrit. Le tester sur une base simulée ne prouve rien sur ce qui
l'intéresse — l'atomicité, le scoping, les conflits d'unicité. Il se teste donc avec
`db_session`, en injectant des doublures **uniquement** pour ce qui est hors de la
machine : le fournisseur de modèle, Open Food Facts, l'horloge.

### 3.3 L'infra se teste par son contrat

Un adaptateur n'a pas de tests « à lui ». Il a un contrat, commun à toutes les
implémentations du même port, et il le passe ou non (§5). C'est ce qui garde le coût
d'ajout d'un fournisseur borné.

Pour les dépôts SQLAlchemy, le contrat est la base réelle ; pour les clients HTTP
sortants (Open Food Facts), c'est un transport `httpx.MockTransport` alimenté par des
réponses **enregistrées**, jamais rédigées à la main.

### 3.4 L'API se teste in-process

Client ASGI, sans serveur ni port ouvert. Ce qu'on y vérifie est ce qui n'existe qu'à
ce niveau : le code de statut, la validation d'entrée aux frontières, la résolution
du tenant depuis le contexte d'authentification, et la forme de la réponse. Pas la
règle métier, qui a déjà été testée là où elle vit.

La fixture `api_client` existe déjà et **skippe** : il n'y a ni fabrique
d'application, ni dépendance de session à surcharger, ni contexte d'authentification
d'où tirer un `HouseholdScope`. Sa docstring liste les trois. Deviner l'un d'eux
produirait une fixture qui teste autre chose que l'application.

---

## 4. Isolation multi-tenant : un dispositif, pas des tests ponctuels

C'est la régression la plus grave (un foyer lit le stock d'un autre — l'inventaire
complet d'un domicile est la donnée la plus sensible de la base) et la plus facile à
introduire (un `select(Item)` sans clause `where` compile, passe le typage, et
fonctionne parfaitement en développement mono-foyer).

Des tests ponctuels ne suffisent pas : ils ne couvrent que les ressources auxquelles
quelqu'un a pensé, et la fuite vient toujours de la table à laquelle personne n'a
pensé. Quatre niveaux, dont trois sont systématiques par construction.

### 4.1 Garde de schéma — automatique, sans base, sur toutes les tables

`backend/tests/tenancy/test_schema_tenant_guard.py` lit `Base.metadata` et vérifie,
table par table, index par index :

| Garde | Ce qu'elle attrape |
|---|---|
| Toute table métier porte `household_id` | La table ajoutée le mois prochain sans colonne de tenant |
| `household_id` est non nul | Une ligne qui n'appartient à personne, invisible plutôt que partagée |
| Toute contrainte d'unicité est scopée | `UNIQUE (barcode)` : empêche le second foyer d'enregistrer sa ligne, **et** lui confirme qu'un autre foyer possède déjà cette valeur |
| Toute référence vers une table du tenant est composite | Un identifiant deviné suffit sinon à écrire dans les données d'un autre foyer, sans qu'aucun bug applicatif ne soit nécessaire |

Le mécanisme est paramétré sur `metadata.sorted_tables` : il n'y a rien à penser à
ajouter. Les exceptions sont des listes nommées, chacune portant sa raison
(`GLOBAL_TABLES`, `NULLABLE_TENANT_TABLES`, `UNIQUE_CONSTRAINT_EXEMPTIONS`,
`SIMPLE_FOREIGN_KEY_EXEMPTIONS`). Y ajouter une entrée est un geste délibéré, visible
en revue — c'est exactement l'effet recherché.

`SIMPLE_FOREIGN_KEY_EXEMPTIONS` est un **cliquet**, pas une absolution : il gèle les
neuf références non composites qui existent aujourd'hui (dont le trou connu sur
`product`, documenté en `data-model.md` §5.2) et empêche la dixième d'arriver
inaperçue. Il a vocation à rétrécir. Une entrée devenue inutile déclenche un
avertissement, pas un échec : faire échouer le commit qui *corrige* quelque chose est
le plus sûr moyen de faire supprimer la garde.

### 4.2 Fixture d'amorçage — la seconde tenant n'est jamais oubliée

`tenant_pair` fournit deux foyers complets et sans lien, chacun avec son
propriétaire. Un test d'isolation qui construit lui-même son second foyer finit
régulièrement par ne pas le construire du tout, et prouve alors qu'un foyer ne peut
pas lire ses propres données depuis un foyer qui n'existe pas.

### 4.3 Test d'isolation par ressource — obligatoire, refus de revue sinon

Pour chaque ressource exposée : le foyer A opère sur les identifiants du foyer B et
reçoit `404`. **Jamais `403`**, qui confirmerait l'existence de la ressource et
transformerait l'API en oracle d'énumération. Vaut pour les lectures comme pour les
écritures (ADR-0006).

Ces tests doivent être générés à partir d'une table de ressources plutôt qu'écrits un
par un : dès que trois ressources existent, une paramétrisation
`(méthode, gabarit d'URL, fabrique)` couvre automatiquement chaque nouvelle entrée,
et l'oubli devient un ajout manquant dans une table unique — visible — plutôt qu'un
fichier de test jamais écrit.

### 4.4 Les jobs de fond, angle mort à traiter à part

Le parsing de ticket et les notifications tournent **hors requête HTTP**, donc hors
du scope applicatif résolu à la frontière. Ce sont eux qui fuiront en premier
(`data-model.md` §5.4). Chaque tâche de fond a son propre test d'isolation :
elle charge le foyer depuis la ligne traitée, jamais depuis un contexte ambiant, et
le test le prouve en traitant une ligne du foyer B pendant qu'un contexte du foyer A
est actif.

### 4.5 Ce que ce dispositif ne prouve pas

Que le filtrage est appliqué par la base. Il ne l'est pas encore : RLS est reporté et
son déclencheur est explicite (ADR-0006 — le jour où un compte est créé par une
personne extérieure au cercle familial). Tant qu'il l'est, l'étanchéité repose sur
une convention applicative, et ces tests sont ce qui la tient. Le jour de
l'activation de RLS, ils deviennent le filet qui prouve que les policies ne cassent
rien — leur valeur augmente, elle ne disparaît pas.

---

## 5. Conformité des adaptateurs LLM

ADR-0005 accepte cinq adaptateurs sur la foi d'une garantie : *« ajouter un
fournisseur, c'est écrire un adaptateur et faire passer cette suite »*. Sans elle,
l'ADR le dit lui-même, cinq adaptateurs seraient imprudents pour un projet solo.
`backend/tests/contracts/test_llm_provider_contract.py` est cette suite.

### 5.1 Le contrat

Un adaptateur conforme honore quatre choses, et la suite vérifie les quatre pour
chacun des cinq fournisseurs :

1. **Signature des ports.** L'implémentation est substituable à `RecipeSuggester` ou
   `ReceiptExtractor` : le domaine ne connaît jamais l'implémentation concrète, et la
   méthode est bien une coroutine (toute la pile est asynchrone).
2. **Traduction des erreurs.** Chaque mode d'échec du fournisseur devient l'exception
   de domaine correspondante — `ProviderUnavailable` (connexion refusée, délai
   dépassé, 5xx), `ProviderQuotaExceeded` (limite de débit, quota épuisé),
   `ProviderResponseInvalid` (charge utile malformée, schéma violé). L'assertion
   décisive n'est pas le type levé mais **son module** : une exception dont le module
   ne commence pas par `chaudron.` a franchi la frontière, et c'est le
   `except AnthropicError` égaré dans une route que l'ADR cherche à éviter. On
   vérifie aussi que le message n'est pas vide : le diagnostic de support a besoin du
   fournisseur, du modèle et du mode d'échec.
3. **Déclaration de capacités bien formée.** Un booléen par capacité
   (`structured_output`, `vision`, `prompt_caching`, `long_context`), une provenance
   `static` ou `probed`, et une date de sondage horodatée avec fuseau si — et
   seulement si — la provenance est `probed`. La provenance est portée par la valeur
   parce que seule une capacité sondée peut périmer : l'interface doit pouvoir
   proposer un rafraîchissement, ce qui n'a aucun sens pour une capacité statique.
4. **Conformité à la taxonomie de dégradation.** Pour chaque capacité déclarée
   absente, l'adaptateur déclare **exactement un** des trois cas de l'ADR, et la
   suite vérifie que le comportement correspond :
   - `unavailable` → l'appel échoue avec une erreur **de domaine** portant une raison
     lisible. Jamais une erreur brute de SDK, jamais un JSON inventé à partir d'une
     image qu'aucun modèle n'a vue.
   - `emulated` → l'appel réussit et rend un objet de domaine valide ; la perte est
     dans le taux d'échec, pas dans le type.
   - `degraded` → l'appel réussit **et dit ce qu'il a laissé de côté**, sans quoi
     l'indicateur persistant de mode dégradé n'a rien à afficher.

   L'absence de déclaration est un échec en soi : le but n'est pas qu'une stratégie
   existe, c'est qu'elle ait été *choisie*. Une capacité manquante sans cas déclaré
   signifie que le comportement est ce que le code fait par accident — précisément ce
   que la taxonomie existe pour empêcher.

Une cinquième garde couvre la complétude : la suite échoue si le registre ne contient
pas les cinq clés d'ADR-0005. Retirer un adaptateur est une révision d'ADR, pas une
ligne discrètement supprimée d'un dictionnaire.

### 5.2 Comment la suite est paramétrée

Deux axes de paramétrisation croisés, et une découverte dynamique :

- une fixture `provider_key` paramétrée sur les cinq clés
  (`anthropic`, `openai`, `gemini`, `mistral`, `ollama`) ;
- une fixture `adapter` qui résout la clé dans le registre
  `chaudron.infra.llm.contract:CONTRACT_ADAPTERS` ;
- des paramétrisations par port, par scénario d'échec et par capacité.

Aujourd'hui le registre n'existe pas : les 145 cas sont collectés et **skippés** avec
la raison. Chaque adaptateur ajouté au registre active sa colonne de la matrice, sans
qu'une ligne du fichier de test soit modifiée. Un adaptateur enregistré mais qui ne
respecte pas la forme attendue **échoue** au lieu d'être skippé : il a choisi
d'entrer dans la suite, il en honore le contrat.

Le registre est découvert par import structurel : l'infrastructure n'importe jamais
le paquet de tests. C'est la contrainte qui garde la règle de dépendance intacte
jusque dans l'outillage.

### 5.3 Sans appeler de vraie API, sans dépenser d'argent

Toute la suite tourne sur **doublures**. Aucune requête réseau, aucun identifiant,
aucun coût, et un résultat déterministe.

Le point important est *où* vit la doublure : **avec l'adaptateur, pas dans le
harnais**. Seul l'adaptateur Anthropic sait à quoi ressemble une limite de débit
Anthropic ; seul l'adaptateur Ollama sait à quoi ressemble une instance injoignable.
Un harnais qui fabriquerait lui-même les réponses testerait sa propre idée des cinq
fournisseurs. Le harnais nomme des *scénarios* (`rate_limited`, `malformed_payload`,
`missing_vision`…) et l'adaptateur fournit, pour chacun, un transport qui le rejoue.

Ces doublures sont construites à partir de **réponses enregistrées** des vrais
fournisseurs (`tests/contracts/recordings/<provider>/`), pas rédigées à la main : un
double inventé à partir de la documentation teste votre lecture de la documentation.
Les enregistrements sont expurgés de tout identifiant avant d'être versionnés — clé
d'API, jeton, identifiant d'organisation, en-têtes de requête. Un enregistrement est
un artefact versionné : il passe par la même revue et le même scan de secrets que le
reste.

### 5.4 Ce qui doit malgré tout être vérifié en réel

Les doublures reposent sur une hypothèse qui se dégrade : que le comportement du
fournisseur n'a pas changé. Or les SDK publient des versions cassantes, les modèles
sont dépréciés, les charges utiles d'erreur sont remaniées. ADR-0005 le liste comme
conséquence négative assumée : *cinq SDK à suivre, et il faut le détecter avant
l'utilisateur*. Rien d'autre que des appels réels ne le détecte.

| | Ce qui est vérifié | Quand | Où |
|---|---|---|---|
| **Fumée réelle** | La clé authentifie, le modèle répond, la sortie structurée valide le schéma, les jetons et le coût sont remontés | Nocturne, sur `main` | Job dédié, hors CI de PR |
| **Fidélité des enregistrements** | Les formes d'erreur réelles correspondent encore aux enregistrements rejoués (rafraîchissement des captures) | Hebdomadaire | Job dédié, avec revue humaine du diff |
| **Sondage Ollama** | Une instance réelle déclare ses capacités comme attendu | Nocturne, instance locale de CI | Job dédié |
| **Qualité d'extraction** | Le jeu d'évaluation de tickets (§8) | Hebdomadaire, et avant tout changement de prompt | Job dédié |

Règles de ce périmètre, non négociables :

- **Jamais sur une pull request.** Un contributeur externe n'a pas de clés, et une CI
  qui dépense de l'argent à chaque push est une CI qu'on finit par contourner. Elle
  serait aussi non déterministe : un test rouge parce qu'un fournisseur est en
  incident apprend quelque chose sur le fournisseur, rien sur le code.
- **Marqueur `live_provider`, désactivé par défaut**, avec un opt-in explicite par
  variable d'environnement et une clé par fournisseur lue depuis l'environnement.
- **Plafond de dépense** et modèles les moins chers de chaque fournisseur : ces tests
  vérifient un protocole, pas une qualité.
- **Un échec notifie, il ne bloque pas un déploiement.** La distinction est ce qui
  garde le signal crédible.

---

## 6. Données de test et fixtures

**Fabriques, pas de jeux de données partagés.** Chaque test construit ce dont il a
besoin via `make_household`, `make_user`, `make_member`, tous arguments facultatifs
avec des valeurs uniques par défaut. Un fichier de données commun chargé pour toute
la suite crée un couplage invisible : un test finit par dépendre d'une ligne qu'un
autre test a introduite pour une raison sans rapport, et personne n'ose plus y
toucher.

**Isolation par transaction, pas par nettoyage.** `db_session` ouvre une transaction
sur la connexion et y rattache la session avec
`join_transaction_mode="create_savepoint"` : le code testé peut appeler `commit()`
librement, l'annulation finale efface tout. Pas de `TRUNCATE` entre les tests, pas de
schéma recréé, pas d'ordre d'exécution significatif — et
`test_previous_test_left_nothing_behind` échoue le jour où ce mécanisme cesse de
fonctionner.

**Référentiels globaux.** `unit` et `llm_provider` sont hors tenant et alimentés par
migration en production. Quand une migration de graine existera, les tests devront
la rejouer plutôt que réinsérer ces lignes à la main : une graine de test qui diverge
de la graine de production est un faux positif permanent.

**Deux valeurs limites à cabler dès qu'elles ont un sens**, parce que ce sont les
pièges connus du domaine (`data-model.md` §6) : une quantité qui doit rester exacte
en décimal (jamais un flottant), et une date de péremption calendaire dans un foyer
dont le fuseau n'est pas celui du serveur.

**Le schéma de test est temporaire.** Il est aujourd'hui créé par
`metadata.create_all`, faute de révision Alembic. Dès que `migrations/versions` aura
du contenu, la fixture devra exécuter les migrations : sinon la suite valide un
schéma qu'aucun environnement n'applique, et une migration cassée arrive en
production avec une CI verte.

---

## 7. Couverture

**Seuil : 85 % de lignes et de branches sur `domain/` et `services/`, 70 % global.**
À activer dans `[tool.coverage.report]` (`fail_under`) au premier cas d'usage
implémenté — l'activer maintenant, sur un dépôt sans code applicatif, mesurerait le
vide et donnerait un chiffre rassurant qui ne veut rien dire. La mesure elle-même est
déjà en place (`--cov`, `--cov-branch`), pour que la courbe existe dès le premier
commit.

Les seuils sont différenciés parce que les couches ne portent pas le même risque : le
domaine concentre les règles et n'a aucune excuse de couverture ; l'API est
majoritairement de la déclaration ; l'infrastructure est couverte par les contrats,
pas par la ligne.

### Ce que la couverture ne dit pas

- **Qu'une ligne exécutée a été vérifiée.** Un test qui appelle une fonction sans
  rien affirmer sur son résultat produit exactement la même couverture qu'un bon
  test. C'est la faiblesse principale de la métrique, et elle est structurelle.
- **Que les bons cas ont été choisis.** 100 % de couverture d'une conversion d'unités
  testée uniquement sur des kilogrammes ne dit rien sur les millilitres, les pièces,
  ou la conversion inter-dimensions qui est le vrai piège du domaine.
- **Que les chemins d'erreur sont corrects.** Une branche `except` couverte prouve
  qu'elle a été empruntée, pas que l'erreur remontée est exploitable par l'appelant
  ou lisible par l'utilisateur.
- **Que l'étanchéité entre foyers est assurée.** Une requête sans filtre de tenant est
  couverte à 100 % par le test qui l'utilise depuis un seul foyer. C'est exactement
  la raison d'être du §4 : la couverture est aveugle à cette classe de défaut.
- **Que le système fonctionne.** Chaque unité peut être couverte et le système
  inutilisable, parce que le défaut est dans l'assemblage.

En conséquence : le seuil est un plancher qui empêche la dérive, jamais un objectif.
Un module sous le seuil déclenche une question, pas un test écrit pour la barre.

---

## 8. Chemins non déterministes

Un modèle de langage rend une réponse différente à chaque appel, et cette réponse
n'est pas la nôtre. La stratégie tient en une phrase : **isoler l'indéterminisme dans
un segment aussi mince que possible, tester tout le reste normalement.**

### 8.1 Découper le chemin en trois

Le flux « inventaire → suggestions » se décompose en trois segments, dont deux sont
parfaitement déterministes :

1. **Construction de la requête** — sérialisation du stock, assemblage du prompt,
   placement du point de coupe pour le cache. Déterministe : test de référence
   (*golden*) sur la sortie exacte. Ces tests attrapent le vrai risque de ce
   segment — une modification de prompt qui déplace le préfixe stable et fait perdre
   le cache, ce qui multiplie le coût sans changer un seul résultat visible.
2. **L'appel** — non déterministe, et le seul segment concerné. Doublure partout
   (§5.3), vrai fournisseur en job dédié (§5.4).
3. **Validation et intégration** — la sortie du modèle est traitée **comme une entrée
   hostile**, au même titre qu'un formulaire posté par un inconnu (architecture §5).
   Déterministe : on teste la validation avec des sorties malformées — JSON tronqué,
   champ manquant, quantité négative, unité inconnue, devise inventée, texte
   d'excuse à la place du JSON, prose enveloppant le JSON, valeur numérique en
   chaîne. Ce segment est celui qui protège l'utilisateur, et c'est le mieux testable
   des trois.

### 8.2 Ne jamais affirmer sur le texte, toujours sur les invariants

Là où une vraie sortie de modèle est en jeu, les assertions portent sur des propriétés
qui doivent tenir quelle que soit la réponse : le schéma valide, les quantités sont
strictement positives, les unités appartiennent au référentiel, les ingrédients
référencent du stock existant, la devise fait trois lettres majuscules, aucun champ
n'est une chaîne vide. Jamais « la réponse contient le mot *poêle* ».

Température zéro et graine fixe sont utilisées quand le fournisseur les propose,
mais **jamais comme fondement d'une assertion** : elles réduisent la variance, elles
ne la suppriment pas, et deux des cinq fournisseurs ne garantissent rien à ce sujet.

### 8.3 Jeu d'évaluation, séparé de la suite de tests

La qualité d'extraction d'un ticket est une **mesure**, pas une assertion. Un jeu
d'une trentaine de photos de tickets réels (anonymisés : ni nom, ni numéro de carte,
ni adresse) avec les lignes attendues, scoré en rappel et précision sur les libellés
et les quantités. On suit la courbe entre deux versions de prompt ou deux modèles, on
compare ; on ne fait pas échouer une PR parce qu'un modèle a lu « PDT NOUV 1KG »
autrement que la semaine dernière.

Ce jeu sert aussi de garde-fou produit : c'est lui qui dit si l'écran de revue reste
nécessaire — et la réponse est oui, ce que le taux de correction humaine mesuré en
production (architecture §7) confirmera ou non.

### 8.4 Ce que la revue humaine change pour les tests

Rien n'entre en stock sans revue humaine (architecture §3.2). C'est ce qui déplace le
risque : un modèle qui se trompe produit une ligne à corriger, pas un stock faux. Les
tests doivent donc porter en priorité sur **le chemin de revue** — qu'une ligne non
revue ne puisse jamais atteindre le stock, qu'une correction soit conservée, qu'un
refus n'écrive rien. C'est déterministe, c'est critique, et ça ne dépend d'aucun
modèle.

Cas particulier : **les allergènes**. Une information allergène issue d'un modèle
n'est jamais présentée comme faisant autorité (architecture §6). Un test dédié doit
vérifier que toute donnée d'allergène d'origine modèle porte sa provenance et son
avertissement jusqu'à la sortie de l'API — une erreur ici a des conséquences
physiques.

---

## 9. Exécution et intégration continue

La CI existante (`.github/workflows/ci.yml`) enchaîne lint → format → mypy → pytest
sur un service `postgres:16` → image Podman → audit de dépendances et scan de
secrets. La stratégie s'y insère sans la modifier : `CHAUDRON_DATABASE_URL` est déjà
posée par le job de test, et les fixtures la préfèrent à tout démarrage de conteneur.

En local, aucune variable n'est nécessaire : un PostgreSQL 16 éphémère est démarré
via testcontainers sur le socket Podman rootless. Deux pièges de cette combinaison
sont traités dans `conftest.py` et documentés dans `tests/README.md` — Ryuk, le
conteneur de nettoyage qui ne démarre pas en rootless et fait échouer la session avec
un message qui accuse PostgreSQL ; et le module `testcontainers.postgres`, un
adaptateur déprécié dont la stratégie d'attente sonde la base depuis l'hôte avec un
pilote synchrone absent, et rapporte le manque comme un refus de connexion de Podman.

Séparation attendue des jobs à mesure que le projet grossit :

| Job | Contenu | Déclencheur |
|---|---|---|
| PR | Lint, types, tests déterministes (base incluse), contrats sur doublures | Chaque push |
| Nocturne | Fumée réelle des fournisseurs, sondage Ollama | `main`, planifié |
| Hebdomadaire | Fidélité des enregistrements, jeu d'évaluation | Planifié |

Un test à durée non bornée ou dépendant du réseau public n'entre jamais dans le job
de PR. Une boucle de retour lente ou instable finit contournée, et une CI contournée
ne protège plus rien.

---

## 10. Ce qui reste ouvert

- Le test d'architecture qui interdit les imports d'infrastructure dans `domain/`
  (§3.1) : à écrire dès que `domain/` contient de la logique.
- Le passage de `metadata.create_all` aux migrations Alembic dans les fixtures (§6).
- L'activation de `fail_under` (§7), au premier cas d'usage implémenté.
- La paramétrisation des tests d'isolation par table de ressources (§4.3), dès la
  troisième ressource exposée.
- L'outillage de bout en bout côté PWA — hors périmètre de ce document, qui ne
  couvre que le backend.
- La stratégie d'authentification n'est pas tranchée (architecture §8) ; les tests
  d'autorisation attendent cette décision, et la fixture `api_client` avec eux.
