# Pantry — revue de sécurité du baseline

> Audit de l'existant au **3 août 2026**, avant la première publication du dépôt.
> Rédigé en français ; les identifiants cités (fichiers, colonnes, variables) sont
> en anglais et font foi tels quels.
> Compagnon : [`security-model.md`](security-model.md), qui décrit la cible.
> **Ce document rapporte. Aucune correction n'a été appliquée.**

---

## 1. Méthode et périmètre

Le dépôt est en phase de cadrage : documentation, ADR, squelette de modèle de
données, unités de conteneurs, CI. **Aucun code de fonctionnalité.** L'audit
porte donc sur la **conception** et le **baseline**, pas sur une implémentation.

Périmètre couvert : les 37 fichiers qui seront effectivement publiés (liste
obtenue en appliquant `.gitignore`), soit `README.md`, `SECURITY.md`,
`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `LICENSE`, `.gitignore`,
`.editorconfig`, `.env.example`, `.github/**`, `backend/**` (hors caches et
`.venv`), `docs/**`, `ops/**`.

Conséquence de conception assumée : les constats portant sur une décision (RLS,
rétention, signature de webhook) sont notés comme des défauts **de conception**.
À ce stade, corriger un document coûte une après-midi ; corriger le schéma
correspondant coûtera une migration.

**Échelle de sévérité.** Elle qualifie l'impact du produit **s'il est livré tel
que conçu**, pas l'exploitabilité aujourd'hui — rien n'est exploitable
aujourd'hui, il n'y a pas d'application.

| Niveau | Signification |
|---|---|
| **Critique** | Compromet un actif appartenant à un tiers, ou l'isolation entre foyers. Bloque la phase 2. |
| **Élevée** | Annule ou contredit un contrôle de sécurité annoncé par le projet. |
| **Moyenne** | Durcissement manquant, ou écart entre deux documents qui produira un défaut. |
| **Faible** | Défaut réel mais à impact borné, ou friction opérationnelle. |
| **Informationnel** | Cohérence, hygiène, dette documentaire. |

---

## 2. Scan de secrets — résultat

### 2.1 Verdict

> ## **Aucun secret réel n'a été trouvé. Le dépôt est propre. Le scan n'est pas un bloqueur de publication.**

### 2.2 Outillage

`gitleaks`, `trufflehog` et `osv-scanner` **ne sont pas installés** sur le poste
(`which` négatif pour les trois). Conformément à la consigne, le scan a été fait
**manuellement** avec `grep -rInE`, sur l'intégralité de l'arbre, `.git/`,
`backend/.venv/`, `backend/.mypy_cache/`, `backend/.ruff_cache/`,
`backend/.pytest_cache/` et `__pycache__/` exclus, puis rejoué **fichier par
fichier sur les 37 fichiers réellement publiables**.

Note favorable : la CI exécute `gitleaks/gitleaks-action@v2` sur l'historique
complet (`fetch-depth: 0`, `.github/workflows/ci.yml:194-206`). Le contrôle
automatisé existe donc dans le pipeline, il manque seulement sur le poste.

### 2.3 Motifs recherchés

`AKIA…`, `ASIA…`, `ghp_`, `gho_`, `ghs_`, `github_pat_`, `sk-`, `sk-ant-`,
`AIza…`, `xox[baprs]-`, `glpat-`, `-----BEGIN`, `PRIVATE KEY`, `AGE-SECRET-KEY`,
`eyJhbGciOi` (JWT), URL à identifiants intégrés `://user:pass@`, et
affectations du type `password|secret|token|api_key|credential = <valeur de 8+
caractères>`. Une passe complémentaire a cherché les chaînes base64/hex de 40
caractères et plus.

### 2.4 Occurrences relevées, et pourquoi aucune n'est un secret

| Fichier:ligne | Valeur | Verdict |
|---|---|---|
| `.env.example:20` | `postgresql+asyncpg://user:password@host:5432/dbname` | **Gabarit.** Littéralement `user` et `password`. |
| `CONTRIBUTING.md:130` | `postgresql+asyncpg://pantry:<password>@127.0.0.1:5432/pantry` | **Gabarit.** Le mot de passe est un chevron à remplacer. |
| `.github/workflows/ci.yml:111` | `postgresql+asyncpg://pantry:pantry@localhost:5432/pantry_test` | **Identifiants d'un service conteneurisé éphémère**, créé et détruit dans le job. Aucune valeur hors CI. Voir SEC-025. |
| `.github/workflows/ci.yml:112` | `PANTRY_SECRET_KEY: ci-only-not-a-real-secret` | **Valeur auto-documentée**, sans entropie. Correcte. |
| `.env.example:61` | `PANTRY_LLM_DEFAULT_MODEL=claude-opus-5` | **Nom de modèle**, pas un secret. Voir SEC-027. |
| `.env.example:89` | `PANTRY_OFF_BASE_URL=https://world.openfoodfacts.org` | URL publique. |

### 2.5 Vérifications complémentaires

- **Historique git : vide.** `git log` ne retourne aucun commit (`No commits yet
  on main`). Il n'y a donc **aucun objet git à réécrire ni à purger** : le
  premier push publiera exactement l'état auditable ci-dessus. C'est la situation
  la plus favorable possible, et elle ne se reproduira pas.
- **`.git/config` :** ne contient qu'un `user.email`, aucun remote, aucun jeton,
  aucun credential helper. Voir SEC-029.
- **Fichiers ignorés présents sur le disque** (`.venv/`, `.coverage`,
  `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `__pycache__/`) : **tous
  correctement exclus** par `.gitignore`. Aucun n'apparaît dans la liste des
  fichiers publiables.
- **`.env` : absent du disque.** Rien à fuiter.
- **`backend/uv.lock` :** aucune correspondance sur les motifs de clés ; ne
  contient que des empreintes SHA-256, ce qui est son objet.
- **Aucun fichier binaire** dans l'ensemble publiable hormis `uv.lock` (texte).
  Pas d'archive, pas de dump, pas de certificat, pas de clé privée.

**Conclusion :** la publication n'est pas bloquée par une fuite de secret. Elle
est bloquée par les constats de la §3 ci-dessous.

---

## 3. Constats

### Critique

---

#### SEC-001 — L'isolation entre foyers repose sur une convention applicative, sans garde-fou moteur

**Sévérité :** Critique
**Fichiers :** `docs/adr/0006-multi-tenant-from-day-one.md:41,49,54` ·
`docs/data-model.md:816-862` (§5.2, §5.3) ·
`backend/src/pantry/domain/models.py:245-256` (`HouseholdScopedMixin`), `:847-851`
(`ix_receipt_pending`), `:416-418` (`product.household_id` nullable) ·
`CONTRIBUTING.md:373-377`

**Description.** Le dispositif d'isolation a trois couches. Les deux premières
sont réelles mais ne couvrent pas la menace principale, et la troisième est
reportée.

Les **FK composites** `(household_id, x_id) → parent(household_id, id)` sont un
excellent contrôle : elles rendent une **écriture** croisée impossible au niveau
de la base, y compris avec un bug applicatif. Sur `llm_purpose_binding`, c'est ce
qui empêche de dépenser la clé d'API d'un autre foyer. Ce point est bien conçu et
doit être conservé.

Mais la fuite qui détruit ce produit est une **lecture** : un `SELECT` sans
`WHERE household_id`. Aucune FK ne filtre une lecture. Restent donc :

1. la revue de code — assurée par un mainteneur unique, qui relit son propre code ;
2. les tests d'isolation obligatoires — écrits par la même personne, pour les
   ressources auxquelles elle a pensé. **Rien n'échoue quand on oublie d'écrire
   le test.** C'est exactement le mode d'échec que l'ADR-0006 reproche à la
   migration tardive, reproduit un cran plus bas.

Trois aggravations concrètes dans le schéma actuel :

- **Les jobs de fond** (parsing de tickets, notifications de péremption,
  réconciliation) s'exécutent **hors requête HTTP**, donc hors du `HouseholdScope`.
  `docs/data-model.md:881-883` le dit explicitement : *« ce sont eux qui fuiront
  en premier »*. L'index `ix_receipt_pending` est **délibérément transverse aux
  foyers** : le worker lit une file mélangée et doit se rescoper à la main sur
  chaque ligne.
- **`product_id` est une FK simple**, car `product.household_id` est nullable et
  ne peut donc pas servir de cible composite. Rien au niveau base n'empêche de
  référencer le produit *privé* d'un autre foyer — ce qui expose des habitudes
  d'achat nominatives (marques, régimes, produits médicaux). Trou connu et assumé
  (`docs/data-model.md:832-838`).
- **Le déclencheur d'activation du RLS n'est pas observable** : « le jour où un
  compte est créé par une personne extérieure au cercle familial ». Ni la CI ni
  un test ne peuvent le constater. Il sera franchi un soir, par commodité.

**L'argument de report ne tient pas pour la pile choisie.** Le RLS est différé
parce que `SET LOCAL` imposerait un pooling en mode transaction, et qu'une
erreur produirait une fuite inverse — une connexion recyclée conservant le foyer
précédent. Ce raisonnement est juste **en présence d'un pooler externe**
(PgBouncer en mode session ou statement). La pile retenue est SQLAlchemy 2.x
asynchrone + `asyncpg`, avec un pool **en processus** qui réserve une connexion
pour la durée de la transaction ; `SET LOCAL` est réinitialisé par PostgreSQL
lui-même au `COMMIT`/`ROLLBACK`. Le mode d'échec redouté suppose soit un `SET` de
session au lieu d'un `SET LOCAL`, soit un composant que Pantry n'a pas choisi.
**Le coût invoqué est celui d'une architecture qui n'est pas la sienne.**

**Impact concret.** Un utilisateur légitime d'un autre foyer lit l'inventaire
complet d'un domicile (`recipe_suggestion.stock_snapshot`), les tickets de
caisse, la liste de courses et la configuration de fournisseur d'un tiers. C'est
la fuite la plus probable et la plus grave de ce produit, et le dépôt public
indiquera précisément où chercher.

**Correction.** **Exiger le RLS dès la v1**, dans la première migration Alembic :

1. Rôle applicatif `pantry_app`, **non propriétaire** des tables, plus
   `ALTER TABLE … FORCE ROW LEVEL SECURITY` sur chaque table portant un
   `household_id` (sans `FORCE`, le propriétaire contourne les politiques).
2. `SET LOCAL app.household_id = …` émis par **un point unique** — la fabrique de
   session — dans la transaction ; discipline « une requête HTTP = une
   transaction », déjà listée comme prérequis à payer immédiatement.
3. Politiques avec `USING` **et** `WITH CHECK` identiques :
   `household_id = current_setting('app.household_id', true)::uuid`. Un
   `current_setting` absent doit faire **échouer** la requête, pas la laisser
   passer.
4. Rôle `pantry_worker` distinct pour la file transverse : une vue ou une
   fonction `SECURITY DEFINER` n'exposant que `(id, household_id)` des tickets en
   attente ; le traitement réel n'a lieu qu'après avoir posé le tenant.
   `ix_receipt_pending` reste transverse mais ne donne plus accès aux données.
5. **Conserver** la couche applicative et les tests d'isolation : le RLS est une
   seconde barrière, pas un remplacement. Un filtre applicatif absent donnera
   alors une requête lente, pas une requête fausse.
6. Trancher en même temps le durcissement de `product_id`
   (`docs/data-model.md:1263`, question 4) : sentinelle `household_id` sur le
   catalogue public, ou `CHECK` par fonction.

Mettre à jour l'ADR-0006 par un **nouvel ADR qui le remplace** (les ADR sont
immuables, `CONTRIBUTING.md:351-356`), corrigeant la prémisse sur le pooling.

**Coût aujourd'hui : une migration et une dépendance de session.** Il n'y a aucun
code de fonctionnalité. Chaque heure de rétrofit que l'ADR-0006 redoute est une
heure qui n'a pas encore été dépensée — c'est l'argument de l'ADR-0006 lui-même,
appliqué à sa propre conclusion.

---

### Élevée

---

#### SEC-002 — La clé de chiffrement des credentials n'est provisionnée par aucun secret Podman

**Sévérité :** Élevée
**Fichiers :** `ops/pantry.container:32,40-42` · `ops/README.md:160-190` ·
`.env.example:38-58` · `docs/adr/0007-byok-and-local-inference.md:42` ·
`docs/data-model.md:1111-1116`

**Description.** `PANTRY_CREDENTIAL_ENCRYPTION_KEY` est déclarée **requise**
(`.env.example:44`) et l'ADR-0007 comme le modèle de données affirment qu'elle
provient de l'environnement **via un secret Podman**, jamais de la base, pour
qu'un dump volé ne contienne rien d'exploitable.

Or le quadlet ne déclare que **trois** secrets — `PANTRY_SECRET_KEY`,
`ANTHROPIC_API_KEY`, `PANTRY_INBOUND_EMAIL_WEBHOOK_KEY` (lignes 40-42) — et la
procédure d'`ops/README.md:167-181` n'en crée que quatre, sans celle-ci.
L'opérateur qui suit la documentation n'a qu'un endroit où la mettre :
`EnvironmentFile=%h/pantry/pantry.env` (ligne 32), **un fichier en clair, dans le
même répertoire personnel que les sauvegardes** produites par `ops/README.md:257`.

Le contrôle décrit — « un dump volé ne suffit pas à déchiffrer » — repose
entièrement sur le fait que la clé et le dump ne voyagent pas ensemble. En
suivant la documentation, ils voyagent ensemble.

**Écart secondaire :** `OPENAI_API_KEY`, `GEMINI_API_KEY` et `MISTRAL_API_KEY`
(`.env.example:56-58`) ne sont pas non plus provisionnées, alors que quatre
fournisseurs sont visés en v1 (ADR-0005). Le quadlet est resté à l'époque où
Anthropic était seul.

**Correction.**

1. Ajouter au quadlet, à la suite des lignes 40-42 :

   ```ini
   Secret=pantry-credential-encryption-key,type=env,target=PANTRY_CREDENTIAL_ENCRYPTION_KEY
   Secret=pantry-openai-api-key,type=env,target=OPENAI_API_KEY
   Secret=pantry-gemini-api-key,type=env,target=GEMINI_API_KEY
   Secret=pantry-mistral-api-key,type=env,target=MISTRAL_API_KEY
   ```

2. Ajouter la commande correspondante dans `ops/README.md` §2.3, au même format
   sûr que les autres (saisie masquée, stdin, `unset`), à exécuter **sur le
   serveur, sous le compte `pantry`** :

   ```sh
   read -rs -p 'Credential encryption key: ' V && printf '%s' "$V" | podman secret create pantry-credential-encryption-key - && unset V
   ```

3. Déplacer les quatre clés fournisseur hors de `.env.example` §« instance_owner »
   vers une note renvoyant aux secrets Podman, pour que le gabarit ne suggère plus
   de les écrire dans un fichier.
4. Ajouter à `ops/README.md` §4 l'avertissement explicite : **les sauvegardes de
   la base et la clé de chiffrement ne doivent pas être stockées au même
   endroit**, faute de quoi le chiffrement au repos ne protège plus de rien.

---

#### SEC-003 — Les colonnes d'erreur peuvent recevoir une clé d'API en clair et l'afficher

**Sévérité :** Élevée
**Fichiers :** `backend/src/pantry/domain/models.py:978` (`last_error`), `:831`
(`parse_error`), `:830` (`raw_response`) · `docs/data-model.md:731,764-765` ·
`docs/adr/0007-byok-and-local-inference.md:44`

**Description.** L'ADR-0007 promet que les clés ne sont *« jamais journalisées »*
et que *« les traces d'exception renvoyées au client sont réécrites — un SDK qui
inclurait la clé dans un message d'erreur ne doit pas la propager »*.

Le schéma contredit cette promesse. `llm_provider_config.last_error` est une
colonne `text` destinée à recevoir le message d'erreur amont, et
`docs/data-model.md:763-765` prévoit qu'elle alimente le bandeau *« votre clé ne
fonctionne plus »*, **affiché sur toutes les pages**. `receipt.parse_error` a la
même forme, et `receipt.raw_response` reçoit la sortie brute d'un endpoint.

Aucune redaction n'est spécifiée **à l'écriture**. Le chemin est direct : un
fournisseur, un proxy d'entreprise, ou un endpoint sous contrôle d'un tiers, qui
renvoie l'en-tête `Authorization` ou la clé dans son message d'erreur, et un
`last_error = str(exc)` écrit dans le service. La clé se retrouve alors **en
clair en base**, puis **à l'écran**, puis **dans le dump `pg_dump`** — c'est-à-dire
exactement là où tout le dispositif de chiffrement au repos servait à ce qu'elle
ne soit pas. C'est la seule faille du projet capable d'annuler la §6.1 du modèle
de menace avec une seule ligne de code.

**Correction.**

1. Ces trois colonnes ne reçoivent **jamais** un texte amont brut. Le service
   traduit l'erreur en un code de domaine (`ProviderUnavailable`,
   `ProviderQuotaExceeded`, `ProviderResponseInvalid`, déjà définis par
   l'ADR-0005) et n'écrit que ce code plus un message contrôlé.
2. Si un extrait amont doit absolument être conservé pour le diagnostic : le
   faire passer par une fonction de redaction unique et testée, qui masque tout
   motif de secret connu (`sk-`, `sk-ant-`, `AIza`, `Bearer …`, en-tête
   `x-api-key`) **et** toute chaîne d'entropie élevée de plus de 20 caractères ;
   borner la longueur stockée.
3. Ajouter un `comment=` sur `last_error` et `parse_error` dans le modèle, au même
   titre que celui déjà porté par `api_key_ciphertext` (`models.py:952-957`), qui
   est un bon exemple à généraliser.
4. Ajouter à la suite de tests un cas explicite : un adaptateur factice qui
   renvoie une erreur contenant une fausse clé, et l'assertion que ni la base ni
   la réponse HTTP ne la contiennent.

---

#### SEC-004 — Deux sources de vérité contradictoires pour l'autorisation `instance_owner`

**Sévérité :** Élevée
**Fichiers :** `.env.example:52` (`PANTRY_INSTANCE_OWNER_HOUSEHOLD_ID`) ·
`backend/src/pantry/domain/models.py:273,280-285`
(`household.is_instance_owner`, `uq_household_instance_owner`) ·
`docs/adr/0007-byok-and-local-inference.md:29` · `docs/data-model.md:1193-1196`

**Description.** Le mode `instance_owner` détermine **qui a le droit de dépenser
l'argent de l'exploitant**. Deux mécanismes différents prétendent le décider :

- l'ADR-0007 : *« utilisable que par le foyer explicitement désigné comme
  propriétaire de l'instance (**variable d'environnement dédiée**) »* — d'où
  `PANTRY_INSTANCE_OWNER_HOUSEHOLD_ID` ;
- le modèle de données : la colonne `household.is_instance_owner`, protégée par
  un index unique garantissant qu'il y en a **au plus un**.

Rien ne dit lequel fait foi, ni comment ils sont maintenus cohérents. Toute
divergence est une autorisation accordée par erreur : un foyer marqué en base
mais absent de l'environnement, ou l'inverse, et l'exploitant paie l'inférence
d'un tiers. Le modèle de données reconnaît par ailleurs que la règle est
inter-tables, donc non exprimable en `CHECK`, et repose sur le service seul.

**Correction.** Choisir **une** source et la rendre subordonnée.

- Retenir `household.is_instance_owner` comme **seule** autorité — l'index unique
  est un vrai garde-fou, ce que l'environnement n'est pas.
- Conserver `PANTRY_INSTANCE_OWNER_HOUSEHOLD_ID` uniquement comme **assertion de
  démarrage** : au boot, si la variable est renseignée et ne correspond pas au
  foyer marqué en base, **refuser de démarrer** (cohérent avec la règle
  fail-fast de `.env.example:3`). Si elle est vide, le mode est désactivé.
- Documenter la procédure d'attribution comme un geste d'administration explicite
  et journalisé.
- Doubler d'une politique RLS lors de l'application de SEC-001.

---

#### SEC-005 — Webhook email : rejeu non traité, adresse de foyer devinable, aucune modélisation

**Sévérité :** Élevée
**Fichiers :** `.env.example:93-101` · `docs/architecture.md:153-163` ·
`SECURITY.md:124-130` · `backend/src/pantry/domain/models.py:264-286`
(`Household`, sans colonne d'adresse entrante) · note de conception absente

**Description.** C'est le seul endpoint conçu pour être appelé par un inconnu, et
c'est la surface sensible la plus sous-spécifiée du dépôt. Quatre manques
distincts :

1. **Rejeu.** La signature est prévue (`PANTRY_INBOUND_EMAIL_WEBHOOK_KEY`), mais
   rien ne parle d'horodatage signé, de fenêtre de tolérance, ni de cache
   d'identifiants de message. Une signature valide capturée reste valide
   indéfiniment et peut être rejouée N fois.
2. **Comparaison non constante.** L'algorithme n'est pas spécifié. Un `==` sur
   une signature est vulnérable au timing ; `hmac.compare_digest` est obligatoire
   et doit être écrit, pas supposé.
3. **Devinabilité de l'adresse de destination — le point le plus grave.**
   L'architecture rattache un email à un foyer *« par l'adresse de destination »*.
   Cette adresse est donc, de fait, **un secret d'autorisation** : quiconque la
   connaît injecte dans ce foyer. Si elle dérive du nom du foyer ou d'un
   compteur, elle est devinable et énumérable — et le dépôt public dira
   exactement comment elle est construite. **Aucune colonne du modèle de données
   ne la porte aujourd'hui**, donc rien ne garantit qu'elle sera aléatoire.
4. **Énumération.** Rien n'impose une réponse identique pour une adresse connue
   et inconnue. Sans cela, le webhook devient un oracle d'existence de foyer.

**Aggravant :** la note de conception censée couvrir tout cela,
`docs/technical-notes-ingestion.md`, **n'existe pas** (voir SEC-019).

**Correction.**

1. Ajouter à `household` une colonne `inbound_email_token` : jeton aléatoire
   d'**au moins 128 bits**, unique, indexé, régénérable, nullable (le foyer qui
   n'utilise pas la fonction n'en a pas). L'adresse devient
   `<token>@<PANTRY_INBOUND_EMAIL_DOMAIN>` et ne dérive **jamais** du nom du
   foyer ni d'un identifiant séquentiel.
2. Spécifier la signature : HMAC-SHA-256 sur `timestamp + corps brut`,
   `hmac.compare_digest`, fenêtre de tolérance de 5 minutes, table de
   déduplication des identifiants de message avec purge.
3. Réponse et délai **identiques** pour une adresse inconnue et une adresse
   connue.
4. Bornes de pièces jointes : nombre maximal, dimensions d'image vérifiées
   **avant** décodage, type MIME déterminé par inspection du contenu et non par
   l'en-tête, et **clé de stockage dérivée de `(household_id, uuid)` — jamais du
   nom de fichier reçu**.
5. Écrire `docs/technical-notes-ingestion.md` **avant** d'implémenter la
   fonction.

---

#### SEC-006 — La validation SSRF décrite ne ferme ni le TOCTOU DNS, ni le port, ni les notations alternatives

**Sévérité :** Élevée
**Fichiers :** `.env.example:71-81` · `docs/adr/0007-byok-and-local-inference.md:47` ·
`docs/architecture.md:190-193` · `SECURITY.md:99-109`

**Description.** Le choix de l'allowlist explicite est **correct**, et le
raisonnement qui l'amène (le filtrage des plages privées est inopérant, puisque
l'adresse légitime d'un Ollama colocalisé *est* privée) est juste. Ce sont les
détails d'application qui manquent, et une allowlist n'est sûre que si l'hôte
autorisé est exactement l'hôte contacté.

Cinq lacunes :

1. **DNS rebinding.** Le contrôle annoncé est *« résolution DNS effectuée à la
   validation et avant l'appel »*. Résoudre deux fois ne ferme pas la fenêtre :
   le client HTTP re-résout au moment de la connexion. Le seul contrôle qui tient
   est **résoudre, valider l'IP, puis se connecter à cette IP**, en portant le
   nom d'origine dans l'en-tête `Host`.
2. **Port libre.** `.env.example:76-79` accepte « hostnames **or** host:port ».
   Un hôte autorisé sans port autorise **tous** ses ports : le serveur devient un
   scanner de ports interne (`ollama:22`, `ollama:5432`, `ollama:6379`), les
   temps de réponse suffisant à cartographier.
3. **Notations alternatives.** Aucune normalisation n'est décrite. `0x7f000001`,
   `2130706433`, `127.1`, `0.0.0.0`, `[::1]`, `[::ffff:127.0.0.1]`, `localhost.`
   (point final), `127.0.0.1.nip.io` contournent toute comparaison textuelle sur
   l'hôte.
4. **`userinfo`.** `http://ollama@attaquant.example/` est lu comme autorisé par un
   parseur naïf.
5. **Pas de plancher de refus.** L'allowlist étant entièrement à la main de
   l'opérateur, rien n'empêche `169.254.169.254` d'y figurer par erreur ou par
   copier-coller.

**Correction.** Une fonction unique, testée, traversée par **tous** les appels
sortants vers un hôte fourni par l'utilisateur — y compris le sondage de
capacités à l'enregistrement :

```
resolve_and_validate(url) -> (ip, port, host_header)
```

- schéma strictement `http`/`https` ; rejet si l'URL contient `@`, un caractère
  de contrôle, ou une séquence encodée dans la partie hôte ;
- **port obligatoire** dans l'allowlist ; une entrée sans port signifie « 11434
  uniquement », jamais « tous » ;
- résolution, puis comparaison sur **l'IP normalisée** (pas sur le texte), puis
  connexion à cette IP avec `Host` explicite ;
- **denylist plancher non contournable**, appliquée même si l'opérateur autorise
  l'hôte : `169.254.0.0/16`, `::ffff:169.254.0.0/112`, `fd00:ec2::254`,
  `0.0.0.0/8`, `::/128` ;
- `follow_redirects=False` posé explicitement sur le client `httpx` ;
- borne de **taille** de réponse — ajouter `PANTRY_OLLAMA_MAX_RESPONSE_BYTES`, il
  n'existe aujourd'hui qu'une borne de temps — et borne de profondeur du JSON
  désérialisé ;
- jeu de tests contenant nommément chacune des notations ci-dessus.

---

#### SEC-007 — L'algorithme JWT est configurable et le secret de signature est partagé entre deux usages

**Sévérité :** Élevée
**Fichiers :** `.env.example:29-35`

**Description.** Deux défauts distincts au même endroit.

`PANTRY_JWT_ALGORITHM` est une **variable d'environnement**. Rendre l'algorithme
de vérification configurable est le point d'entrée classique de la confusion
d'algorithme : `none`, ou une signature RSA vérifiée comme un HMAC avec la clé
publique. L'algorithme d'une application est une propriété du code, pas de son
déploiement — un opérateur n'a aucune raison légitime de le changer, et le rendre
modifiable ne crée qu'un risque.

`PANTRY_SECRET_KEY` est décrite comme servant *« to sign sessions and JWTs »* :
un seul secret pour deux mécanismes. Sa fuite compromet les deux, et sa rotation
invalide les deux.

**Correction.**

1. Retirer `PANTRY_JWT_ALGORITHM` de `.env.example`. Figer l'algorithme dans le
   code, et **imposer une liste d'algorithmes acceptés** au décodage
   (`algorithms=["HS256"]`), jamais celui annoncé par le jeton.
2. Séparer les secrets : soit deux variables, soit une clé maîtresse et deux
   sous-clés dérivées par HKDF avec des contextes distincts.
3. Trancher la stratégie d'authentification (`docs/architecture.md:247-248`)
   **avant** la première migration : transport (cookie `Secure`, `HttpOnly`,
   `SameSite=Lax` ou `Strict`), durée, et **mécanisme de révocation** — un JWT
   sans liste de révocation ne se retire pas avant expiration.

---

#### SEC-008 — Aucune rétention n'est définie, et le `CASCADE` ne supprime pas les images

**Sévérité :** Élevée
**Fichiers :** `backend/src/pantry/domain/models.py:800` (`image_object_key`),
`:830` (`raw_response`), `:1080` (`stock_snapshot`), `:245-256` (`CASCADE`) ·
`docs/data-model.md:1265-1267` (question 5) · `docs/architecture.md:223,249`

**Description.** Deux problèmes qui se combinent mal.

**Aucune durée de rétention n'est fixée**, et surtout **aucune colonne ne permet
de la suivre**. L'architecture recommande *« purge après traitement à
privilégier »* et le modèle de données classe la question comme à trancher avant
le premier compte tiers. Sans colonne, il n'y a pas de purge : il y a une
intention. Cela concerne les trois données les plus sensibles du système : les
images de tickets, `receipt.raw_response`, et `recipe_suggestion.stock_snapshot`
— que le modèle qualifie lui-même de *« donnée la plus sensible de la base »*,
puisque c'est **l'inventaire complet d'un domicile, figé**.

**Le `ON DELETE CASCADE` ne couvre que PostgreSQL.** Il est décrit comme
répondant à l'effacement RGPD, *« totale et atomique, pas un script de nettoyage
qui oublie une table »* — et c'est vrai pour la base. Mais supprimer un foyer
efface les lignes et **laisse tous les objets** du stockage. Un effacement
partiel présenté comme complet est une non-conformité qui a l'air d'une
conformité.

**Correction.**

1. Trancher les durées **avant la première migration**, et les inscrire dans le
   schéma. Proposition à arbitrer : image purgée à la confirmation de la revue
   (30 jours maximum), `raw_response` 90 jours, `stock_snapshot` **30 jours**,
   `raw_label` conservé après dissociation du foyer, journaux 30 jours.
2. Ajouter les colonnes qui rendent la purge vérifiable : `image_purged_at` sur
   `receipt`, et un `expires_at` (ou une politique dérivée de `created_at`)
   exploitable par une tâche planifiée.
3. Écrire la suppression de foyer comme une **opération applicative** :
   énumération et suppression des objets **avant** le `DELETE` de la ligne
   `household`, avec vérification et journalisation. Le `CASCADE` reste utile
   pour l'atomicité en base ; il ne doit plus être présenté comme l'effacement
   RGPD complet.
4. Supprimer les métadonnées EXIF (GPS, appareil, horodatage) **à l'ingestion**,
   avant écriture — sinon la géolocalisation du domicile est conservée et
   re-servie.

---

#### SEC-009 — Aucune limitation de débit n'est conçue, nulle part

**Sévérité :** Élevée
**Fichiers :** `.env.example` (aucune variable) · `backend/pyproject.toml:15-26`
(aucune dépendance) · `docs/architecture.md:216-227` (absent du tableau
sécurité) · `SECURITY.md:142-148`

**Description.** Aucun des quatre endpoints qui en ont besoin n'a de limitation
de débit décrite :

- **connexion** — bourrage d'identifiants et force brute, l'attaque par défaut
  d'un visiteur non authentifié sur une instance auto-hébergée sans WAF ni
  fail2ban ;
- **webhook email** — endpoint public, amplification et saturation ;
- **upload de ticket** — coût CPU, disque, et appel de modèle **facturé** ;
- **génération de recette** — chaque appel coûte de l'argent au foyer, ou à
  l'exploitant en mode `instance_owner`, où `PANTRY_LLM_MONTHLY_BUDGET_USD`
  n'est qu'un plafond **global** : atteint, il coupe la fonction pour tout le
  monde.

S'y ajoute le plafond Open Food Facts : 15 requêtes/minute **par IP**, donc
global à l'instance, avec bannissement à la clé — un seul foyer qui scanne en
rafale peut couper la résolution produit pour tous les autres. Le client sortant
est limité à 10 req/min (ADR-0008), ce qui protège Open Food Facts d'une
surcharge mais ne protège pas les autres foyers de la monopolisation du quota.

**Correction.**

1. Décider maintenant le mécanisme (compteur PostgreSQL avec fenêtre glissante,
   suffisant à cette échelle et sans dépendance nouvelle) et l'inscrire au
   modèle de menace.
2. Limites par identité **et** par IP sur la connexion, avec verrouillage
   temporaire progressif et journalisation.
3. Quota par foyer sur la génération de recette et l'import de ticket,
   configurable, avec un message honnête plutôt qu'un 429 nu.
4. File d'attente équitable par foyer devant le client Open Food Facts, pour
   qu'un foyer ne puisse pas consommer tout le quota d'instance.
5. Réponses et délais identiques à la connexion pour un email connu et inconnu
   (anti-énumération).

---

### Moyenne

---

#### SEC-010 — Le démarrage rapide du README publie PostgreSQL sur toutes les interfaces

**Sévérité :** Moyenne
**Fichier :** `README.md:166-169`

**Description.** Le bloc de démarrage rapide contient `-p 5432:5432`, qui publie
la base sur **toutes** les interfaces de l'hôte. C'est le bloc le plus copié-collé
d'un dépôt public, et il contredit directement `ops/README.md:96`
(`-p 127.0.0.1:8000:8000`) et `ops/pantry-db.container:26-28`, qui insiste à juste
titre : *« The database is never published to the host »*. Le mot de passe
aléatoire (`openssl rand -hex 16`) limite le dommage sans supprimer l'exposition
(scan, empreinte de version, vulnérabilités futures du démon).

**Correction.** Remplacer par `-p 127.0.0.1:5432:5432`, et ajouter la même note
d'une ligne que dans `ops/` expliquant pourquoi la boucle est le défaut.

---

#### SEC-011 — Les actions GitHub tierces ne sont pas épinglées par empreinte

**Sévérité :** Moyenne
**Fichier :** `.github/workflows/ci.yml:36,68,117,179` (`astral-sh/setup-uv@v7`),
`:204` (`gitleaks/gitleaks-action@v2`), `:33,65,114,148,176,198,218`
(`actions/checkout@v5`), `:134`, `:232`

**Description.** Toutes les actions sont référencées par **tag de version
majeure**. Un tag est mutable : son déplacement par un mainteneur compromis ou
malveillant exécute du code arbitraire dans le runner, avec accès au dépôt en
lecture et au cache de dépendances. Le risque est plus élevé sur les deux actions
**tierces** (`astral-sh/setup-uv`, `gitleaks/gitleaks-action`) que sur celles de
l'organisation `actions`.

**Correction.** Épingler par SHA de commit complet, avec le tag en commentaire :

```yaml
uses: astral-sh/setup-uv@<sha-40-caractères>  # v7.x.y
uses: gitleaks/gitleaks-action@<sha-40-caractères>  # v2.x.y
```

Ajouter un `.github/dependabot.yml` avec l'écosystème `github-actions`, qui
proposera les montées de SHA en pull request plutôt que de les subir en silence.

---

#### SEC-012 — Images de base épinglées par tag, et mise à jour automatique de la base de données depuis le registre

**Sévérité :** Moyenne
**Fichiers :** `backend/Containerfile:14,36` · `ops/pantry-db.container:19-20`

**Description.** Deux écarts avec la discipline appliquée partout ailleurs dans
le projet, où les dépendances Python sont épinglées **exactement** avec un
lockfile et `UV_FROZEN`.

1. Les images de base (`ghcr.io/astral-sh/uv:python3.14-bookworm-slim`,
   `python:3.14-slim-bookworm`) sont épinglées par tag mutable. Le commentaire de
   tête du `Containerfile` revendique *« bump deliberately, never implicitly »* —
   un tag ne permet pas de tenir cette promesse.
2. `AutoUpdate=registry` sur `docker.io/library/postgres:16` fait **tirer
   automatiquement** une nouvelle image de base de données dès qu'elle est
   publiée, sans revue, sans fenêtre de maintenance, et sans sauvegarde vérifiée
   au préalable. C'est un redémarrage non planifié du composant le plus difficile
   à restaurer, déclenché par un tiers. Le quadlet de l'API utilise
   `AutoUpdate=local` (ligne 21), plus prudent — l'asymétrie n'est pas justifiée.

**Correction.**

1. Épingler les trois images par `@sha256:…`, tag en commentaire, montée
   explicite. Confier le suivi à Dependabot (écosystème `docker`, qui gère les
   `Containerfile`).
2. Remplacer `AutoUpdate=registry` par `AutoUpdate=local` sur
   `pantry-db.container`, ou le retirer. Une montée de version de base de données
   est une opération planifiée, précédée d'une sauvegarde vérifiée —
   `ops/README.md` §4 décrit déjà la bonne procédure.

---

#### SEC-013 — `.gitignore` n'exclut ni les sauvegardes, ni les clés, ni le fichier d'environnement de production

**Sévérité :** Moyenne
**Fichiers :** `.gitignore:14-21` · `ops/README.md:257` · `ops/pantry.container:32`

**Description.** Trois manques, dont un directement induit par la documentation.

1. **Aucun motif de sauvegarde.** `ops/README.md:257` propose
   `pg_dump … > pantry-$(date -I).dump` **dans le répertoire courant**. Exécutée
   depuis le dépôt — ce qui est le réflexe naturel —, la commande dépose une
   copie complète de la base (A3, A4, A1 chiffrés) dans un répertoire de travail
   git non ignoré.
2. **Aucun motif de matériel cryptographique** : `*.pem`, `*.key`, `*.p12`,
   `*.pfx`, `id_rsa*`.
3. **`pantry.env` n'est pas couvert.** Les règles ignorent `.env` et `.env.*`,
   mais le fichier d'environnement de production s'appelle `pantry.env`
   (`ops/pantry.container:32`) et ne correspond à aucun motif.

**Correction.** Ajouter à la section « Secrets and local environment » :

```gitignore
*.env
pantry.env
*.dump
*.sql
*.sql.gz
*.pem
*.key
*.p12
*.pfx
id_rsa*
```

et modifier `ops/README.md:257` pour écrire la sauvegarde dans un répertoire
dédié hors dépôt (`~/pantry/backups/`), avec un rappel sur son chiffrement et sa
séparation d'avec la clé de chiffrement (voir SEC-002).

---

#### SEC-014 — Le contenu Open Food Facts est stocké brut et rendu comme s'il était fiable

**Sévérité :** Moyenne
**Fichiers :** `backend/src/pantry/domain/models.py:419-438` (`gtin`, `name`,
`brand`, `category_tag`, `image_url`, `off_payload`) ·
`docs/architecture.md:208-209`

**Description.** L'architecture pose la bonne règle — *« sortie de modèle traitée
comme entrée non fiable »* — mais ne l'applique qu'aux modèles. Or les champs
issus d'Open Food Facts sont **rédigés par des contributeurs anonymes** : `name`,
`brand`, `category_tag` sont du texte libre tiers, `off_payload` est un instantané
brut conservé en JSONB, et `image_url` est une URL tierce.

C'est exactement la même classe de risque, sur un chemin qu'on ne surveille pas
parce qu'il n'a pas l'air d'être de l'IA : XSS stocké si un nom de produit est
rendu en HTML, chargement de ressource distante non maîtrisée si `image_url` est
utilisée telle quelle par le client. Le projet a par ailleurs déjà identifié que
les données Open Food Facts sont peu fiables (*« no assurances that the data is
accurate »*), mais côté **qualité**, pas côté **innocuité**.

**Correction.**

1. Ajouter explicitement les données Open Food Facts au périmètre de la règle
   « entrée non fiable » dans `docs/architecture.md` §5.
2. Validation Pydantic stricte à l'entrée : longueurs bornées, jeu de caractères
   contrôlé, `image_url` restreinte au schéma `https` et au domaine
   `images.openfoodfacts.org`.
3. Rendu en **texte pur** côté PWA, jamais en HTML ni en Markdown avec liens
   actifs, plus une CSP stricte sans `unsafe-inline`.
4. Ne pas charger `image_url` depuis le client : proxy et cache côté serveur,
   comme déjà recommandé pour la charge (`technical-notes-scanning.md` §3.5,
   point 6). La raison sécurité s'ajoute à la raison de courtoisie.
5. Borner la taille de `off_payload` avant persistance.

---

#### SEC-015 — CORS : aucun garde-fou contre l'association origine générique et identifiants

**Sévérité :** Moyenne
**Fichier :** `.env.example:103-109`

**Description.** `PANTRY_CORS_ORIGINS` est une liste explicite, ce qui est
correct. Mais `PANTRY_CORS_ALLOW_CREDENTIALS` existe sans aucune contrainte
documentée. L'association `*` + `allow_credentials=True` est le défaut de
configuration CORS le plus courant et le plus destructeur : n'importe quel site
lit alors les réponses authentifiées de l'API.

**Correction.**

1. Faire **échouer le démarrage** — et non produire un avertissement — si
   `PANTRY_CORS_ORIGINS` contient `*` alors que
   `PANTRY_CORS_ALLOW_CREDENTIALS=true`. C'est cohérent avec la règle fail-fast
   déjà annoncée en tête de `.env.example`.
2. Ne **jamais** refléter l'en-tête `Origin` dans `Access-Control-Allow-Origin` :
   seule une valeur de la liste configurée est émise.
3. Le documenter dans le commentaire de `.env.example`, où un opérateur le lira.

---

#### SEC-016 — L'algorithme de hachage des mots de passe n'est ni décidé ni outillé

**Sévérité :** Moyenne
**Fichiers :** `backend/src/pantry/domain/models.py:301` · `backend/pyproject.toml:15-26`

**Description.** `user_account.password_hash` est un `text` nullable. Aucun
document ne dit avec quoi il est produit, et **aucune dépendance de hachage n'est
présente** dans `pyproject.toml` (ni `argon2-cffi`, ni `passlib`, ni `bcrypt`).
En l'absence de décision, le premier développeur qui implémente la connexion
choisira sous pression — et c'est ainsi qu'on obtient du SHA-256 salé à la main.

Manquent également : le suivi des tentatives échouées, le re-hachage à la
connexion quand les paramètres évoluent, et la longueur maximale acceptée (une
absence de borne est un déni de service sur un algorithme lent).

**Correction.**

1. Trancher **Argon2id**, avec des paramètres explicites et versionnés, et
   ajouter `argon2-cffi` en dépendance épinglée.
2. Stocker le hachage au format PHC (`$argon2id$v=19$m=…`), qui porte ses propres
   paramètres et rend le re-hachage progressif possible sans colonne
   supplémentaire.
3. Re-hacher à la connexion réussie quand les paramètres stockés diffèrent des
   paramètres courants.
4. Borner la longueur du mot de passe accepté (par exemple 4096 octets).

---

#### SEC-017 — Les contrôles de sécurité de la CI ne bloquent rien et ne s'exécutent jamais d'eux-mêmes

**Sévérité :** Moyenne
**Fichier :** `.github/workflows/ci.yml:1-7,169-206` · absence de
`.github/dependabot.yml` · absence de `.github/CODEOWNERS`

**Description.** Les deux jobs de sécurité existent et sont bien choisis
(`pip-audit --strict` sur les dépendances verrouillées, `gitleaks` sur
l'historique complet). Trois faiblesses les rendent moins efficaces qu'ils n'en
ont l'air :

1. **Ils ne sont pas déclarés obligatoires.** Aucun `needs:` ne les chaîne aux
   autres jobs, et rien dans le dépôt ne documente une protection de branche ni
   une liste de vérifications requises. Un scan que l'on peut fusionner en échec
   ne protège de rien. `SECURITY.md:165-167` et `CONTRIBUTING.md:313-314`
   affirment pourtant que la CI les impose.
2. **Aucune exécution planifiée.** Les déclencheurs sont `push` sur `main`,
   `pull_request` et `workflow_dispatch`. Sur un projet à faible fréquence de
   commits, une CVE publiée le lendemain d'une fusion dort jusqu'à la pull
   request suivante — potentiellement des mois.
3. **Aucun `dependabot.yml`, aucun `CODEOWNERS`.** Les montées de version sont
   entièrement manuelles, et aucune revue n'est requise sur les chemins
   sensibles.

**Correction.**

1. Activer la protection de branche sur `main` : pull request obligatoire,
   `security-deps` et `security-secrets` en vérifications requises, historique
   linéaire.
2. Ajouter `schedule: - cron: "0 6 * * 1"` au workflow pour une exécution
   hebdomadaire de l'audit de dépendances.
3. Ajouter `.github/dependabot.yml` couvrant `github-actions`, `uv` (ou `pip`) et
   `docker`.
4. Ajouter `.github/CODEOWNERS` sur `ops/`, `.github/`, `docs/adr/` et
   `backend/src/pantry/domain/`.
5. Vérifier que `gitleaks/gitleaks-action@v2` ne requiert pas de licence pour ce
   dépôt : ce point conditionne le fonctionnement réel du job, et il est
   silencieux s'il échoue à s'initialiser.

---

#### SEC-018 — Aucune borne sur les uploads HTTP, face à un `/tmp` de 64 Mo

**Sévérité :** Moyenne
**Fichiers :** `ops/pantry.container:60-62` · `.env.example:100-101` ·
`backend/pyproject.toml:25` (`python-multipart`)

**Description.** Le conteneur est en `ReadOnly=true` avec un unique
`Tmpfs=/tmp:rw,size=64M`. C'est un bon durcissement. Mais `python-multipart`
déverse sur disque au-delà d'un seuil, et **aucune borne de taille n'existe pour
l'upload HTTP d'un ticket** : `PANTRY_INBOUND_EMAIL_MAX_BYTES` ne couvre que la
voie email. Un upload volumineux remplit les 64 Mo et fait échouer tout ce qui a
besoin d'écrire.

Manquent également : un plafond de volume total par foyer, et une pagination
obligatoire sur les listes potentiellement longues (`stock_movement`,
`receipt_line`).

**Correction.**

1. Ajouter `PANTRY_RECEIPT_MAX_UPLOAD_BYTES`, appliquée **avant** la lecture du
   corps (refus sur `Content-Length`, plus vérification en flux), et refuser au
   niveau du reverse proxy également.
2. Configurer explicitement le seuil de déversement de `python-multipart` et le
   répertoire temporaire utilisé.
3. Dimensionner `Tmpfs` en conséquence, ou monter un volume dédié aux uploads en
   cours.
4. Pagination obligatoire et plafonnée sur toute liste, dès la première route.

---

#### SEC-019 — `docs/technical-notes-ingestion.md` est référencé deux fois et n'existe pas

**Sévérité :** Moyenne
**Fichiers :** `README.md:206` · `docs/architecture.md:163`

**Description.** Le document est présenté comme couvrant *« Inbound email,
receipt OCR, shopping list export »*, et `docs/architecture.md` y renvoie pour le
détail du webhook. Il est absent. Ce n'est pas seulement un lien mort dans un
dépôt public : **c'est la note de conception de la surface la plus sensible et la
plus sous-spécifiée du projet** (voir SEC-005). Son absence explique en grande
partie pourquoi le rejeu, la devinabilité de l'adresse et l'énumération ne sont
traités nulle part.

**Correction.** Écrire la note avant d'implémenter la fonction, en y traitant
nommément les cinq points de SEC-005. À défaut et à très court terme, retirer les
deux liens plutôt que de publier un dépôt qui promet un document inexistant.

---

#### SEC-020 — Aucune journalisation d'audit des accès aux actifs sensibles

**Sévérité :** Moyenne
**Fichiers :** `backend/src/pantry/domain/models.py` (aucune table d'audit) ·
`docs/architecture.md:229-239`

**Description.** L'observabilité prévue est bonne pour l'exploitation : logs
structurés dès le premier commit, `household_id` et identifiant de requête sur
chaque ligne, trois métriques bien choisies. Mais rien ne trace les **accès aux
actifs sensibles** : déchiffrement d'une clé, création ou modification d'une
configuration de fournisseur, export d'un foyer, suppression d'un foyer,
attribution du mode `instance_owner`, accès à une image de ticket.

Conséquence directe : en cas de violation de données, l'exploitant ne peut ni
délimiter l'incident ni démontrer qu'il n'y en a pas eu — alors qu'il doit
notifier sous 72 heures. C'est aussi le seul moyen de détecter un abus par un
utilisateur légitime, qui par définition ne déclenche aucune alerte
d'authentification.

**Correction.**

1. Ajouter une table `audit_event` append-only : `occurred_at`, `household_id`
   (nullable pour les événements d'instance), `actor_user_id`, `action` (enum
   fermé), `target_type`, `target_id`, `request_id`, `ip_hash`. Pas de contenu,
   seulement des références — une table d'audit ne doit pas devenir un second
   entrepôt de données personnelles.
2. Journaliser au minimum les six événements listés ci-dessus.
3. Rétention distincte et plus longue que celle des journaux applicatifs (12
   mois), et exclusion explicite de la purge RGPD ordinaire — un journal d'audit
   se conserve au titre de l'intérêt légitime, ce qui doit être écrit.

---

### Faible

---

#### SEC-021 — Le fichier d'environnement de production est créé avec le mauvais propriétaire

**Sévérité :** Faible
**Fichier :** `ops/README.md:183-190`

**Description.** `install -m 0600 /dev/null ~pantry/pantry.env` figure dans une
section dont les commandes précédentes s'exécutent en `root` (`useradd`,
`install -d`). Le fichier sera donc **possédé par root en mode 0600**, et le
compte `pantry` — sous lequel tourne le quadlet — ne pourra pas le lire :
`EnvironmentFile=` échouera au démarrage. Le mode 0600 est le bon réflexe ; le
propriétaire ne l'est pas.

**Correction.** `install -o pantry -g pantry -m 0600 /dev/null ~pantry/pantry.env`,
et préciser sous quel compte chaque bloc de la section §2 doit être exécuté — la
distinction est déjà bien faite en §2.3, elle manque en §2.4.

---

#### SEC-022 — L'URL du dépôt est incohérente entre les quadlets et le reste du projet

**Sévérité :** Faible
**Fichiers :** `ops/pantry.container:11` · `ops/pantry-db.container:11`
(`https://github.com/stackops/pantry`) · `README.md:11`, `SECURITY.md:35`,
`CONTRIBUTING.md:75`, `backend/pyproject.toml:29`,
`.github/ISSUE_TEMPLATE/config.yml:11` (`ClaraVnk/pantry`)

**Description.** Les deux unités quadlet pointent vers `stackops/pantry`, tout le
reste vers `ClaraVnk/pantry`. Ce n'est pas qu'une coquille : un opérateur qui
découvre un problème de sécurité en lisant l'unité systemd sur son serveur suivra
le lien `Documentation=` et atterrira sur un dépôt qui n'est pas celui du projet.
Le canal de signalement décrit dans `SECURITY.md` est alors contourné avant même
d'avoir été lu.

**Correction.** Aligner sur l'URL canonique définitive **avant** le premier push
public — c'est le moment où le choix est encore gratuit — et vérifier
l'ensemble des occurrences d'un coup.

---

#### SEC-023 — `DAC_OVERRIDE` sur le conteneur de base de données

**Sévérité :** Faible
**Fichier :** `ops/pantry-db.container:52-53`

**Description.** Le quadlet applique `DropCapability=ALL` puis réintroduit
`CHOWN,DAC_OVERRIDE,FOWNER,SETGID,SETUID`. La démarche est la bonne, et ces
capacités sont effectivement nécessaires au point d'entrée de l'image `postgres`
officielle, qui ajuste les permissions puis abandonne ses privilèges.
`DAC_OVERRIDE` reste néanmoins la plus large de la liste : elle contourne toutes
les vérifications de permissions de fichiers dans le conteneur.

**Correction.** Optionnel et à mesurer, pas à appliquer les yeux fermés : fixer
l'appartenance du répertoire de données sur l'hôte
(`podman unshare chown -R 999:999 ~/pantry/data/postgres`, la technique est déjà
documentée pour les uploads en `ops/README.md:247`), puis retirer `DAC_OVERRIDE`
et `FOWNER` et vérifier que `initdb` **et** un redémarrage passent. Si l'un des
deux échoue, conserver la configuration actuelle et l'annoter — une capacité
justifiée par écrit vaut mieux qu'une capacité retirée qui casse au troisième
démarrage.

---

#### SEC-024 — La CI construit une image à partir d'un `Containerfile` contrôlé par la pull request

**Sévérité :** Faible
**Fichier :** `.github/workflows/ci.yml:143-164`

**Description.** Le job `backend-build` exécute `podman build` sur le
`Containerfile` de la branche, y compris pour une pull request venant d'un fork.
Les instructions `RUN` s'exécutent donc sur le runner avec du code contrôlé par un
contributeur inconnu.

L'impact est **fortement borné** — et c'est pourquoi la sévérité reste faible :
le déclencheur est `pull_request` et non `pull_request_target` (le piège classique
est **correctement évité**), le jeton est en lecture seule
(`permissions: contents: read`), aucun secret de dépôt n'est exposé aux forks, et
les caches d'une PR de fork sont isolés de ceux de la branche de base. Restent le
détournement de calcul et l'accès réseau sortant depuis le runner.

**Correction.** Activer dans les réglages du dépôt *« Require approval for all
outside collaborators »* sur les exécutions de workflow — un clic, et c'est le
contrôle proportionné ici. Ne pas ajouter de secret à ce job ; s'il devait un jour
pousser une image vers un registre, le faire dans un workflow **distinct**
déclenché uniquement sur `push` de `main` ou sur tag.

---

#### SEC-025 — Identifiants de base de test en clair dans le workflow

**Sévérité :** Faible
**Fichier :** `.github/workflows/ci.yml:96-99,111`

**Description.** `POSTGRES_PASSWORD: pantry` et le DSN correspondant sont écrits
en clair. Ce **n'est pas une fuite** : le service conteneurisé naît et meurt avec
le job, il n'est joignable que depuis le runner, et la ligne 108 le documente
correctement. Deux inconvénients subsistent : tout scanner de secrets signalera
cette ligne à perpétuité (bruit qui finit par masquer un vrai signal), et
l'habitude d'écrire un mot de passe en clair dans un workflow public est celle
qui produit la fuite suivante.

**Correction.** Générer la valeur dans le job
(`echo "PGPW=$(openssl rand -hex 16)" >> "$GITHUB_ENV"`) et composer le DSN à
partir d'elle, ou ajouter une exclusion nommée et commentée dans la configuration
`gitleaks`. La seconde option est la moins coûteuse.

---

### Informationnel

---

#### SEC-026 — `PANTRY_CREDENTIAL_ENCRYPTION_KEY` est absente de l'environnement de test

**Sévérité :** Informationnel
**Fichier :** `.github/workflows/ci.yml:107-112`

Le job de tests fournit `PANTRY_ENV`, `PANTRY_LOG_LEVEL`, `PANTRY_DATABASE_URL`
et `PANTRY_SECRET_KEY`, mais pas `PANTRY_CREDENTIAL_ENCRYPTION_KEY`, pourtant
marquée **REQUIRED** dans `.env.example:38-44`. Si la validation fail-fast est
honnête, l'application ne démarrera pas en CI dès qu'un test l'instanciera.
Ajouter une valeur de test explicite (`ci-only-not-a-real-key`), ce qui aura
l'effet secondaire utile de vérifier que la variable est bien obligatoire.

---

#### SEC-027 — `.env.example` porte une valeur, contrairement à sa propre règle

**Sévérité :** Informationnel
**Fichiers :** `.env.example:61,89` · `CONTRIBUTING.md:84,366`

`CONTRIBUTING.md` affirme que `.env.example` *« never carries a value that
matters »* et fait de l'ajout d'une valeur réelle un motif de refus de pull
request. Deux lignes en portent une : `PANTRY_LLM_DEFAULT_MODEL=claude-opus-5` et
`PANTRY_OFF_BASE_URL=https://world.openfoodfacts.org`. Aucune n'est un secret, et
ce sont des valeurs par défaut légitimes — mais la règle telle qu'écrite est déjà
violée par le fichier qu'elle décrit. Soit préciser la règle (« aucune valeur
**secrète** »), soit déplacer ces défauts dans le code de configuration, où ils
ont davantage leur place.

---

#### SEC-028 — Documents de cadrage périmés par rapport aux ADR acceptés

**Sévérité :** Informationnel
**Fichiers :** `docs/architecture.md:245-250,68-69,220`

Le §8 « Ce qui n'est pas encore tranché » liste *« La licence du projet »*, alors
que `LICENSE` est AGPL-3.0 et que `CONTRIBUTING.md` §7 en détaille les
conséquences. Il liste aussi *« La topologie Ollama retenue pour la v1 »*, que
l'ADR-0007 a tranchée (cas colocalisé uniquement). Le tableau des ports (lignes
68-69) ne mentionne qu'Anthropic et Ollama alors que l'ADR-0005 en retient cinq,
et le tableau sécurité (ligne 220) parle de *« la clé de chiffrement »* sans la
nommer.

Un document de cadrage périmé est lu comme s'il était à jour, et c'est ainsi
qu'une décision se retrouve re-litigée. À rafraîchir avant publication — c'est
la première chose qu'un lecteur externe ouvrira (`README.md:28-29` l'y envoie).

---

#### SEC-029 — Identité git incohérente avec l'auteur déclaré du projet

**Sévérité :** Informationnel
**Fichier :** `.git/config` (`user.email = kevin@stackops.ch`) ·
`backend/pyproject.toml:11` (`authors = [{ name = "ClaraVnk" }]`) ·
`CONTRIBUTING.md:440`

Le dépôt n'a **aucun commit** : le premier push figera cette identité dans
l'historique public de chaque commit initial. L'adresse configurée n'est pas
celle de l'auteur déclaré. Ce n'est pas un problème de sécurité applicative, mais
une divulgation d'adresse et une incohérence d'attribution que l'on ne corrige
plus proprement après publication. Vérifier `user.name` et `user.email` **avant**
le premier commit, et envisager une adresse de type `noreply` si l'exposition
n'est pas souhaitée.

---

#### SEC-030 — Pas de politique de signature, pas de SBOM, pas de signature d'image

**Sévérité :** Informationnel

Ni signature des commits ou des tags, ni génération de SBOM à la construction, ni
signature des images publiées. **Rien de tout cela n'est nécessaire aujourd'hui**
— le projet ne publie pas d'artefact — et l'ajouter maintenant serait de la
cérémonie. À réévaluer au premier tag de version : c'est le moment où des tiers
commenceront à exécuter des binaires produits par ce dépôt, et où une chaîne
vérifiable cesse d'être décorative.

---

#### SEC-031 — L'exigence sur les allergènes n'a aucun mécanisme

**Sévérité :** Informationnel
**Fichier :** `docs/architecture.md:225`

Le tableau sécurité contient une ligne juste et importante : *« Une erreur ici a
des conséquences physiques. Ne jamais présenter une information allergène issue
d'un modèle comme faisant autorité »*. C'est la seule menace du projet dont
l'impact soit corporel, et elle n'existe que sous forme de ligne de tableau —
aucun mécanisme, aucune contrainte de schéma, aucun test.

Une phrase dans un document ne survit pas jusqu'à l'écran de recette. À
transformer en contrainte : les allergènes proviennent d'`allergens_tags`
d'Open Food Facts ou d'une saisie humaine, **jamais** d'un champ produit par un
modèle ; le schéma de sortie du modèle ne comporte aucun champ d'allergène ; et
l'affichage porte un avertissement non désactivable. À traiter comme une
exigence produit, pas comme une note.

---

## 4. Tableau de synthèse

| ID | Sévérité | Constat | Fichier principal |
|---|---|---|---|
| SEC-001 | **Critique** | Isolation entre foyers laissée à la convention applicative ; RLS reporté sur une prémisse fausse pour la pile choisie | `docs/adr/0006-…:49` |
| SEC-002 | Élevée | `PANTRY_CREDENTIAL_ENCRYPTION_KEY` non provisionnée en secret Podman ; finit en clair à côté des sauvegardes | `ops/pantry.container:32,40-42` |
| SEC-003 | Élevée | `last_error` / `parse_error` / `raw_response` peuvent recevoir et afficher une clé d'API en clair | `models.py:978,831,830` |
| SEC-004 | Élevée | Deux sources de vérité pour l'autorisation `instance_owner` | `.env.example:52` / `models.py:273` |
| SEC-005 | Élevée | Webhook email : rejeu, adresse de foyer devinable, énumération, aucune modélisation | `docs/architecture.md:153-163` |
| SEC-006 | Élevée | SSRF : TOCTOU DNS, port libre, notations alternatives, pas de plancher de refus | `docs/adr/0007-…:47` |
| SEC-007 | Élevée | Algorithme JWT configurable par l'environnement ; secret de signature partagé | `.env.example:31-35` |
| SEC-008 | Élevée | Aucune rétention définie ; le `CASCADE` ne supprime pas les images | `models.py:800,830,1080` |
| SEC-009 | Élevée | Aucune limitation de débit conçue (connexion, webhook, upload, génération) | `.env.example` (absent) |
| SEC-010 | Moyenne | Le démarrage rapide du README publie PostgreSQL sur toutes les interfaces | `README.md:169` |
| SEC-011 | Moyenne | Actions GitHub tierces épinglées par tag mutable | `ci.yml:36,204` |
| SEC-012 | Moyenne | Images de base par tag ; `AutoUpdate=registry` sur la base de données | `Containerfile:14,36` / `pantry-db.container:20` |
| SEC-013 | Moyenne | `.gitignore` n'exclut ni dumps, ni clés, ni `pantry.env` | `.gitignore:14-21` |
| SEC-014 | Moyenne | Contenu Open Food Facts stocké brut et rendu comme fiable | `models.py:419-438` |
| SEC-015 | Moyenne | CORS : pas de garde-fou origine générique + identifiants | `.env.example:109` |
| SEC-016 | Moyenne | Hachage de mot de passe ni décidé ni outillé | `models.py:301` |
| SEC-017 | Moyenne | Jobs de sécurité non bloquants, pas de scan planifié, pas de Dependabot | `ci.yml:169-206` |
| SEC-018 | Moyenne | Aucune borne d'upload HTTP face à un `/tmp` de 64 Mo | `pantry.container:62` |
| SEC-019 | Moyenne | `docs/technical-notes-ingestion.md` référencé deux fois, inexistant | `README.md:206` |
| SEC-020 | Moyenne | Aucune journalisation d'audit des accès aux actifs sensibles | `models.py` (absent) |
| SEC-021 | Faible | `pantry.env` créé avec le mauvais propriétaire | `ops/README.md:189` |
| SEC-022 | Faible | URL de dépôt incohérente entre quadlets et reste du projet | `ops/*.container:11` |
| SEC-023 | Faible | `DAC_OVERRIDE` sur le conteneur de base de données | `pantry-db.container:53` |
| SEC-024 | Faible | `podman build` d'un `Containerfile` contrôlé par une PR de fork | `ci.yml:143-164` |
| SEC-025 | Faible | Identifiants de base de test en clair dans le workflow | `ci.yml:96-99,111` |
| SEC-026 | Info | `PANTRY_CREDENTIAL_ENCRYPTION_KEY` absente de l'environnement de test | `ci.yml:107-112` |
| SEC-027 | Info | `.env.example` porte des valeurs, contrairement à sa propre règle | `.env.example:61,89` |
| SEC-028 | Info | Documents de cadrage périmés par rapport aux ADR acceptés | `docs/architecture.md:245-250` |
| SEC-029 | Info | Identité git incohérente avec l'auteur déclaré | `.git/config` |
| SEC-030 | Info | Pas de signature de commits, de SBOM, ni de signature d'image | — |
| SEC-031 | Info | L'exigence sur les allergènes n'a aucun mécanisme | `docs/architecture.md:225` |

**Répartition :** 1 Critique · 8 Élevées · 11 Moyennes · 5 Faibles · 6
Informationnels — **31 constats**.

---

## 5. À corriger avant le premier push public

Le classement est celui du **coût de la correction après publication**, pas celui
de la sévérité. Un dépôt public sans commit est dans une situation qui ne se
reproduira pas : tout ce qui est corrigé maintenant n'a jamais existé.

### Bloquant — ne pas pousser sans

Ces cinq points sont soit très bon marché maintenant et coûteux ensuite, soit
susceptibles d'induire un lecteur en erreur dès la première heure de publication.

1. **SEC-029 — vérifier `user.name` et `user.email`.** Cinq secondes. Après le
   premier push, l'identité est dans chaque commit de l'historique public, pour
   toujours.
2. **SEC-013 — compléter `.gitignore`** (`*.dump`, `*.sql`, `pantry.env`, `*.pem`,
   `*.key`) **et** corriger `ops/README.md:257` pour écrire les sauvegardes hors
   du dépôt. C'est la seule correction qui empêche une fuite *future* par simple
   copier-coller de la documentation.
3. **SEC-010 — corriger `-p 5432:5432` en `-p 127.0.0.1:5432:5432`.** Une ligne.
   C'est le bloc le plus copié d'un dépôt public, et il expose une base de
   données.
4. **SEC-022 — aligner l'URL du dépôt.** Un canal de signalement de vulnérabilité
   qui pointe vers le mauvais dépôt est pire qu'absent.
5. **SEC-019 / SEC-028 — soit écrire les documents manquants, soit retirer les
   liens morts et rafraîchir `docs/architecture.md` §8.** Le README envoie tout
   lecteur externe vers ces documents ; c'est la première impression du projet.

### Avant la première ligne de code de fonctionnalité

Ces décisions se prennent une fois et se paient en migration si on les prend
tard. Aucune n'exige d'écrire du code — seulement de trancher et de le consigner.

6. **SEC-001 — trancher le RLS**, et le consigner dans un ADR remplaçant
   l'ADR-0006. La première migration Alembic doit contenir le rôle non
   propriétaire, `FORCE ROW LEVEL SECURITY` et les politiques.
7. **SEC-008 — trancher les durées de rétention** et ajouter les colonnes
   correspondantes à la première migration. Ajouter une colonne à un schéma vide
   est gratuit.
8. **SEC-005 — ajouter `household.inbound_email_token`** au modèle, et écrire
   `docs/technical-notes-ingestion.md` avec le rejeu, l'énumération et les bornes
   de pièces jointes.
9. **SEC-004 — choisir la source de vérité unique** pour `instance_owner`, et
   transformer la variable d'environnement en assertion de démarrage.
10. **SEC-007 / SEC-016 — trancher l'authentification** : Argon2id, algorithme
    JWT figé dans le code, secrets séparés, mécanisme de révocation.
11. **SEC-002 — compléter les secrets Podman** dans le quadlet et la procédure
    d'exploitation. Un quadlet incomplet publié devient le modèle que tout le
    monde recopie.

### Dans les premières semaines

12. **SEC-003** — règle de redaction et test dédié, à écrire **en même temps** que
    le premier adaptateur de fournisseur, pas après.
13. **SEC-006** — écrire `resolve_and_validate` et sa suite de tests **avant** le
    premier appel sortant vers un hôte fourni par l'utilisateur.
14. **SEC-009, SEC-018** — limitation de débit et bornes d'upload, dès la première
    route qui accepte un corps.
15. **SEC-011, SEC-012, SEC-017** — épinglage par SHA, `dependabot.yml`,
    protection de branche avec les jobs de sécurité en vérifications requises.
16. **SEC-014, SEC-015, SEC-020** — contenu externe traité comme non fiable,
    garde-fou CORS au démarrage, table `audit_event`.
17. **SEC-021, SEC-023, SEC-024, SEC-025, SEC-026, SEC-027, SEC-031** — nettoyage
    au fil de l'eau.

---

## 6. Ce que cet audit reconnaît comme bien fait

Un rapport qui n'énumère que des défauts donne une image fausse du baseline, et
il rend le prochain arbitrage plus difficile. Les points suivants sont
au-dessus de ce qu'on voit habituellement à ce stade, et **doivent être
préservés** lors des corrections ci-dessus :

- **Les FK composites** `(household_id, x_id)` sur chaque référence intra-foyer.
  C'est peu coûteux, c'est appliqué par la base, et cela élimine toute une classe
  de fuites par confusion d'identifiant. Sur `llm_purpose_binding`, c'est
  explicitement un contrôle de sécurité, et il est correct.
- **`api_key_ciphertext` déclarée `deferred=True`**, avec un `COMMENT ON COLUMN`
  et une contrainte `CHECK` de cohérence du triplet de secret. Trois dispositifs
  qui agissent sur le développeur qui n'a rien lu — c'est le bon niveau de
  paranoïa, et le modèle à généraliser (voir SEC-003).
- **`ck_llm_provider_config_mode_requirements`**, qui rend vérifiable *par la base
  elle-même* la règle « la clé de l'instance n'est jamais recopiée en base ».
- **Le chiffrement authentifié avec AAD** `(household_id, config_id)` : un chiffré
  recopié sur une autre ligne ne se déchiffre pas. Peu de projets à ce stade y
  pensent.
- **Le durcissement des conteneurs** : rootless, UID fixe, `NoNewPrivileges`,
  `DropCapability=ALL`, `ReadOnly=true`, base jamais publiée, API sur la boucle
  locale.
- **Le traitement de SELinux** : `:Z` avec sa justification, interdiction
  explicite de `setenforce 0`, piège `:U` documenté avec son remède. C'est de la
  documentation d'exploitation de qualité rare.
- **Les procédures de secrets** : saisie masquée, stdin, `unset` final, et le
  piège du saut de ligne documenté.
- **La CI évite `pull_request_target`**, restreint les permissions du jeton,
  utilise `--locked` / `UV_FROZEN`, teste contre un vrai PostgreSQL, et exécute
  `gitleaks` sur l'historique complet.
- **`SECURITY.md`** : périmètre par surface, hors-périmètre explicite, délais
  annoncés comme des engagements d'effort et non de service, et l'instruction de
  ne jamais inclure de secret réel dans un rapport. Le fait d'y déclarer les
  **défauts de conception** dans le périmètre est exactement la bonne posture à
  ce stade du projet.
- **`CONTRIBUTING.md` §6**, dont plusieurs motifs de refus sont directement des
  contrôles de sécurité : pas de `household_id` retiré, pas de tenant dérivé
  d'une entrée client, pas d'affaiblissement de l'allowlist SSRF. Une convention
  écrite ne remplace pas un contrôle moteur (SEC-001), mais elle vaut mieux que
  son absence.
- **La revue humaine obligatoire avant toute écriture en stock**, présentée non
  comme une protection contre les hallucinations mais comme le produit lui-même.
  C'est le contrôle le plus solide du projet contre l'injection de prompt, et il
  tient parce qu'il est aligné avec l'intérêt de l'utilisateur plutôt que contre
  lui.
