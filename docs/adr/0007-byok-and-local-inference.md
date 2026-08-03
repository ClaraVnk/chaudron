# 0007. Clé par foyer (BYOK) et inférence locale

## Statut

Accepté — 2026-08-03

## Contexte

Les fonctionnalités de modèle (suggestions de recettes, extraction de ticket) ont un coût à l'usage. La question n'est pas seulement *combien*, mais *qui paie* et *quel risque cela crée*.

Le modèle par défaut d'une application SaaS — l'exploitant fournit l'accès au modèle et refacture — impose immédiatement : un plafond de dépense global à surveiller, une protection contre l'abus (un foyer qui génère des milliers de recettes vide le budget commun), un système de quotas, à terme une facturation, et la responsabilité du traitement des données de tous les utilisateurs auprès d'un fournisseur tiers.

Pantry est un projet solo, en phase 1 familiale. Aucun de ces chantiers n'est finançable, et chacun serait une source permanente d'incidents.

Par ailleurs, une partie des utilisateurs cibles d'une application auto-hébergée voudra ne rien envoyer à un fournisseur externe. L'inférence locale n'est pas un repli dégradé pour eux : c'est la raison pour laquelle ils installent le produit.

## Décision

**Chaque foyer configure son propre accès au modèle.** Il n'existe aucun mode dans lequel l'application paie pour l'ensemble des utilisateurs. C'est une décision de conception assumée, pas une limitation temporaire.

Trois modes, stockés sur le foyer :

| Mode | Ce que fournit le foyer | Qui paie |
|---|---|---|
| `byok` | Sa propre clé API — **Anthropic, OpenAI, Gemini ou Mistral AI**, les quatre fournisseurs de premier rang de la v1 (cf. ADR-0005) | Le foyer, directement auprès du fournisseur |
| `ollama` | Une URL de base et un nom de modèle, aucune clé | Personne (calcul local) |
| `instance_owner` | Rien : la clé lue dans l'environnement de l'instance | Le propriétaire de l'instance |

**La clé du propriétaire de l'instance est strictement personnelle.** Le mode `instance_owner` n'est utilisable que par le foyer explicitement désigné comme propriétaire de l'instance (variable d'environnement dédiée). Il est **verrouillé par défaut** : tout autre foyer qui tente de le sélectionner reçoit un refus. Un foyer sans configuration valide n'a simplement pas accès aux fonctionnalités de modèle — les autres fonctions de Pantry (stock, liste de courses, scan EAN) restent entières.

**Topologie Ollama — v1 : cas colocalisé uniquement.** Deux topologies existent, et elles ne sont pas réductibles l'une à l'autre :

- *Ollama colocalisé* avec le backend (même hôte, même réseau Podman) : appel serveur → serveur, trivial. **C'est le seul cas supporté en v1.**
- *Ollama sur la machine ou le LAN de l'utilisateur* : le backend ne peut pas l'atteindre, il est derrière un NAT. Le seul composant capable de le joindre est le **navigateur** de l'utilisateur.

Supporter le second cas exige une inversion : le backend renverrait un *bundle de prompt* (prompt rendu, nom de modèle, schéma de sortie attendu), le front l'enverrait à l'Ollama local, puis reposterait la réponse brute au backend pour validation et écriture en base. Le coût est réel : le prompt devient exposable côté client (donc non secret), une seconde voie d'exécution apparaît pour chaque fonctionnalité de modèle, le backend doit valider une réponse dont il ne contrôle pas la provenance et la traiter comme une entrée hostile, et l'utilisateur doit configurer `OLLAMA_ORIGINS` sur son instance pour autoriser le CORS — une étape que le support devra expliquer en permanence.

**Ce coût n'est pas payé en v1.** L'interface documente que le mode `ollama` requiert une instance joignable depuis le serveur. La voie navigateur est le chemin d'extension identifié, pas un travail en cours.

**Sécurité des clés fournies par les foyers.**

- **Chiffrées au repos.** La clé de chiffrement provient de l'environnement (secret Podman), jamais de la base : une fuite de dump ne suffit pas à déchiffrer.
- **Écriture seule via l'API.** Aucun point d'entrée ne renvoie une clé. La lecture retourne uniquement le fournisseur, un horodatage et les **quatre derniers caractères** pour permettre l'identification.
- **Jamais journalisées.** Filtre explicite dans la configuration de journalisation structurée ; les objets de configuration surchargent leur représentation pour masquer la valeur ; les traces d'exception renvoyées au client sont réécrites — un SDK qui inclurait la clé dans un message d'erreur ne doit pas la propager.
- **Rotation.** Remplacer une clé est une écriture idempotente sur le foyer ; l'ancienne valeur est écrasée, pas versionnée. La procédure est documentée dans le `README` et rappelée dans l'interface.

**SSRF.** Dans le cas colocalisé, l'URL Ollama est **fournie par l'utilisateur et appelée par le serveur** : c'est une primitive SSRF. Le filtrage habituel (rejeter les plages privées) est ici inopérant, puisque l'adresse légitime d'un Ollama colocalisé est précisément privée. La validation retenue est donc une **allowlist explicite d'hôtes**, définie par variable d'environnement de l'instance (typiquement le nom de service Podman du conteneur Ollama). En complément : schéma limité à `http`/`https`, résolution DNS effectuée à la validation **et** avant l'appel pour empêcher un DNS rebinding, redirections désactivées, délai d'attente et taille de réponse bornés. Une URL hors allowlist est rejetée à l'enregistrement, avec un message explicite.

**Détection de capacités à la configuration.** L'enregistrement d'une configuration `ollama` déclenche un appel à l'instance pour établir les capacités du modèle déclaré (vision, sortie structurée, taille de contexte), qui sont persistées avec la configuration du foyer — voir ADR-0005, où cette détection dynamique est décrite comme une différence de nature avec `AnthropicProvider` et `GeminiProvider`. Cet appel passe par la même validation SSRF que les appels d'inférence : une instance injoignable ou hors allowlist fait échouer l'enregistrement avec la raison affichée, plutôt que d'enregistrer une configuration dont on ignore ce qu'elle sait faire.

## Conséquences

### Positives

- **Aucun plafond de dépense global à gérer** : il n'y a pas de budget commun à protéger, donc pas de risque d'abus, pas de quotas, pas de facturation à construire.
- La surface RGPD se réduit fortement : Pantry ne devient pas responsable de l'envoi des données de tous ses utilisateurs à un fournisseur tiers ; chaque foyer contracte directement, ou n'envoie rien.
- **Le foyer choisit sa juridiction.** Deux configurations gardent les données de consommation alimentaire sous juridiction européenne : `byok` avec Mistral AI (hébergé en UE) et `ollama` (rien ne sort de la machine). Ce critère est affiché dans l'interface de sélection du fournisseur (cf. ADR-0005), pas enfoui dans la documentation.
- Le mode `ollama` rend un déploiement entièrement autonome possible, sans aucun appel sortant.
- Le contrôle du coût reste entre les mains de celui qui le supporte : plafond de dépense côté fournisseur, choix du modèle, choix du moment.
- Le mode `instance_owner` verrouillé évite le scénario classique où le propriétaire découvre sa facture après avoir partagé son instance.

### Négatives

- **Barrière à l'entrée élevée.** Un nouvel utilisateur doit créer un compte chez un fournisseur, générer une clé et la coller, ou installer Ollama. La majorité des gens abandonnera avant d'avoir vu la première recette suggérée. C'est le prix direct de la décision, et il est lourd.
- **La confusion abonnement / clé d'API est prévisible et coûteuse.** Un utilisateur muni de ChatGPT Plus, Claude Pro/Max ou Gemini Advanced croira légitimement disposer d'un accès. Il n'en a aucun : ce sont des produits distincts, facturés séparément. L'interface doit lever l'ambiguïté au moment de la saisie de la clé (cf. ADR-0005), faute de quoi ce sera la première source de tickets de support.
- **Quatre fournisseurs à choisir, c'est aussi une charge de décision.** Un utilisateur non technique n'a aucun critère pour trancher entre Anthropic, OpenAI, Gemini et Mistral. L'interface doit recommander un défaut et n'exposer les autres qu'en second rang, sinon le choix lui-même devient un point d'abandon.
- **Le produit devient inégal.** L'extraction de ticket sur papier thermique abîmé fonctionne bien avec un modèle propriétaire récent, médiocrement avec un petit modèle ouvert. Deux utilisateurs jugeront le même produit très différemment (cf. ADR-0005, dégradation par capacités).
- **Stocker des clés d'API tierces est une responsabilité.** Même chiffrées, ce sont des secrets à valeur monétaire directe. Le chiffrement au repos ne protège pas d'une compromission applicative, puisque l'application doit déchiffrer pour appeler.
- **L'allowlist SSRF est une friction opérationnelle** : ajouter un hôte Ollama exige de modifier l'environnement de l'instance et de la redémarrer. C'est volontaire — une validation dynamique et permissive rouvrirait exactement le trou que l'allowlist ferme.
- **La topologie limitée à v1 sera perçue comme un défaut.** Un utilisateur avec Ollama sur son portable ne pourra pas l'utiliser, et l'explication (NAT, CORS) est difficile à faire passer.
- **Le support se complique.** Un incident peut venir de leur clé, de leur quota, de leur instance Ollama, de leur `OLLAMA_ORIGINS`, ou du code. Faire remonter le fournisseur et le mode d'échec dans les messages d'erreur est une exigence, pas un confort.

## Alternatives écartées

- **L'application paie pour tous les foyers** — la meilleure expérience d'accueil, de loin. Écartée : impose plafond de dépense, quotas, anti-abus, facturation et responsabilité RGPD étendue, tous non finançables sur un projet solo. C'est un vrai sacrifice d'adoption, assumé.
- **Crédits offerts puis BYOK** (gratuit jusqu'à N requêtes) — atténuerait la barrière à l'entrée. Écartée : réintroduit intégralement le budget commun, l'anti-abus et le suivi de consommation, pour un adoucissement temporaire.
- **Ollama exclusivement, aucun fournisseur externe** — supprime la question des clés. Écartée : la qualité d'extraction de ticket des modèles ouverts ne suffit pas au cas d'usage principal, et l'installation d'Ollama est une barrière au moins aussi haute qu'une clé API.
- **Clés en clair en base** — plus simple à implémenter. Écartée sans discussion : un dump de base suffirait alors à voler des secrets facturables appartenant à des tiers.
- **Voie navigateur pour Ollama dès la v1** — couvrirait toutes les topologies. Écartée pour v1 : double le chemin d'exécution de chaque fonctionnalité de modèle avant que le produit n'ait un seul utilisateur externe.

## Révision

- Implémenter la voie navigateur pour Ollama si des utilisateurs auto-hébergés demandent effectivement à utiliser un Ollama de poste de travail ou de LAN. Le coût est connu et documenté ci-dessus ; seule la demande manque.
- Réévaluer l'accueil si le taux d'abandon à l'étape de configuration du fournisseur est mesurable et élevé : un mode de démonstration à quota très serré, adossé à la clé du propriétaire d'instance et explicitement opt-in par ce dernier, serait alors le compromis minimal à instruire.
- N'ajouter un sixième fournisseur que sur demande d'utilisateur avérée : l'ADR-0005 le permet sans réécriture, mais chaque adaptateur conservé a un coût de maintenance permanent. Symétriquement, retirer un adaptateur qu'aucun foyer n'utilise.
