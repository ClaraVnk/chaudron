# 0005. Abstraction de domaine pour les fournisseurs de modèles

## Statut

Accepté — 2026-08-03

## Contexte

Deux fonctionnalités de Chaudron reposent sur un modèle de langage : la génération de suggestions de recettes à partir du stock disponible, et l'extraction structurée des lignes d'un ticket de caisse photographié (modèle multimodal).

La solution la plus directe serait d'appeler le SDK d'un fournisseur depuis les handlers HTTP. Trois éléments l'excluent :

1. **Chaque foyer configure son propre accès au modèle** (cf. ADR-0007). Il n'existe pas un fournisseur unique décidé par l'application, mais autant de configurations que de foyers, résolues à l'exécution. Le fournisseur est une donnée, pas une constante de déploiement.
2. **Cinq adaptateurs sont visés dès la v1**, quatre de premier rang et un cas dégradé de référence :

   | Adaptateur | Fournisseur | Statut |
   |---|---|---|
   | `AnthropicProvider` | Anthropic (Claude) | pleinement capable — `claude-opus-5` est le modèle par défaut de la documentation et du mode `instance_owner` |
   | `OpenAIProvider` | OpenAI (GPT) | pleinement capable |
   | `GeminiProvider` | Google (Gemini) | pleinement capable |
   | `MistralProvider` | Mistral AI (Mistral, Pixtral) | pleinement capable, **hébergé en UE** |
   | `OllamaProvider` | local | capacités variables, détectées à la configuration |

3. **Le domaine n'a pas besoin de savoir qui répond.** « Proposer des recettes à partir de ce stock » et « extraire les lignes de ce ticket » sont des opérations métier ; le transport, le format des messages et la gestion des jetons sont de l'infrastructure.

Cet ADR pose l'abstraction alors que le projet évite par ailleurs l'abstraction prématurée. La règle de trois ne s'applique pas ici : cinq implémenteurs sont exigés dès la v1, et le choix entre eux se fait par foyer à chaque requête. Ce n'est pas anticiper un changement possible, c'est modéliser une variabilité déjà présente.

## Décision

**Ports de domaine.** Deux interfaces sont définies dans la couche domaine, sans aucune dépendance à un SDK :

- `RecipeSuggester` : à partir d'un inventaire, produit des suggestions de recettes.
- `ReceiptExtractor` : à partir d'une image, produit des lignes d'achat structurées.

Elles n'exposent ni « prompt », ni « message », ni « token » : uniquement des objets du domaine (`InventoryItem`, `RecipeSuggestion`, `ReceiptLine`). Les erreurs des fournisseurs sont traduites en exceptions de domaine (`ProviderUnavailable`, `ProviderQuotaExceeded`, `ProviderResponseInvalid`) ; aucune exception de SDK ne franchit la frontière.

**L'interface est conçue pour le fournisseur le plus capable, pas pour le plus faible.** Elle expose la surface complète — sortie structurée stricte, vision, indices de mise en cache de prompt, taille de contexte — et chaque adaptateur déclare ce qu'il sait en faire. Concevoir au plus petit dénominateur commun alignerait le produit sur le fournisseur le moins capable alors que quatre adaptateurs sur cinq sont pleinement capables : c'est précisément le piège que cette décision écarte.

**Les capacités appartiennent au couple (fournisseur, modèle), pas au fournisseur.** Un fournisseur de premier rang peut servir un modèle sans vision ou à contexte court si l'utilisateur le choisit pour réduire son coût. La taxonomie de dégradation ci-dessous s'applique donc à toute configuration, pas seulement à Ollama — c'est ce qui la garde pertinente avec quatre fournisseurs pleinement capables.

**Taxonomie de dégradation.** Pour chaque capacité manquante, l'adaptateur relève de l'un de ces trois cas exactement. Le choix se fait par couple (capacité × fonctionnalité) et constitue une décision documentée, jamais une conséquence accidentelle du code :

1. **Émulation avec perte documentée** — la capacité est approchée par un autre moyen. Exemple : pas de sortie structurée native → le format JSON attendu est demandé dans le prompt, la réponse est validée côté serveur contre le schéma, avec une politique de reprise bornée. La fonctionnalité reste disponible, le taux d'échec est plus élevé, l'utilisateur en est informé.
2. **Dégradation fonctionnelle visible** — la fonctionnalité reste offerte dans une forme réduite, signalée comme telle dans l'interface (le « mode dégradé »). Exemple : contexte trop court pour l'inventaire complet → suggestions calculées sur un sous-ensemble d'articles, avec mention explicite du périmètre retenu.
3. **Indisponibilité explicite** — la fonctionnalité est désactivée, avec la raison affichée et la marche à suivre pour y remédier. Exemple : pas de vision → l'import de ticket est désactivé, jamais une erreur brute ni un JSON inventé à partir d'un modèle qui n'a pas vu l'image.

**Indicateur de mode dégradé.** Dès que la configuration d'un foyer n'a pas la capacité pleine, l'interface affiche un indicateur **persistant**, détaillant ce qui est réduit ou indisponible et pourquoi. L'utilisateur ne doit jamais découvrir la limite au moment de l'échec : il doit la connaître avant d'essayer. C'est aussi ce qui protège la réputation du produit — une extraction médiocre attribuée au petit modèle local que l'utilisateur a lui-même chargé n'est pas la même chose qu'une extraction médiocre attribuée à Chaudron.

**Modèle de capacités : deux natures de déclaration, explicites dans le type.** L'asymétrie entre adaptateurs est structurante et fait partie du modèle, pas un cas particulier traité au coup par coup :

- **Capacités statiques** (`AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`, `MistralProvider`) — connues à l'avance, dérivées du couple (fournisseur, modèle) par une table embarquée dans l'adaptateur. Aucun appel réseau n'est nécessaire pour les connaître.
- **Capacités sondées** (`OllamaProvider`) — dépendent du modèle chargé dans l'instance de l'utilisateur, qui peut changer sans que Chaudron en soit averti. Elles sont établies à la configuration en interrogeant l'instance, persistées avec la configuration du foyer, horodatées, et rafraîchies sur demande explicite.

Le domaine consomme les deux à travers le même type `ProviderCapabilities`, mais la provenance (`static` / `probed`, avec la date du sondage) est portée par la valeur : l'interface peut ainsi signaler qu'une capacité sondée date et proposer un rafraîchissement, ce qui n'a aucun sens pour une capacité statique.

**Piège de nommage à désamorcer.** Trois fournisseurs vendent un abonnement grand public dont le nom sera confondu avec l'accès API. C'est la première source prévisible de tickets de support, et l'interface de configuration doit lever l'ambiguïté **au moment où l'utilisateur colle sa clé**, avec le lien vers la bonne console :

| L'utilisateur pense… | Il lui faut en réalité… |
|---|---|
| ChatGPT Plus | une clé d'API OpenAI (facturation à l'usage, console développeur) |
| Claude Pro / Max | une clé d'API Anthropic (console développeur) |
| Gemini Advanced | une clé d'API Google AI |

Un abonnement grand public ne donne **aucun** accès programmatique : ce sont deux produits distincts, avec deux facturations distinctes. Le message d'erreur d'une clé invalide doit renvoyer explicitement à cette distinction plutôt qu'au message brut du SDK.

**Souveraineté des données : deux options, à exposer dans le choix du fournisseur.** Deux configurations seulement garantissent que les données de consommation alimentaire d'un foyer ne quittent pas la juridiction européenne : **Mistral AI** (hébergé en UE) et **Ollama** (rien ne sort de la machine). C'est un critère de choix réel pour un utilisateur européen, et il doit être affiché comme une propriété du fournisseur dans l'interface de sélection, au même rang que ses capacités — pas enfoui dans une documentation.

**Adaptateurs.** Les implémentations vivent dans la couche infrastructure. Une fabrique construit l'adaptateur adéquat à partir de la configuration du foyer courant ; les handlers reçoivent le port par injection et ne connaissent jamais l'implémentation concrète.

**Tests de conformité d'adaptateur.** Une suite de contrat unique, paramétrée sur tous les adaptateurs, définit ce qu'un adaptateur doit honorer : respect de la signature des ports, traduction de chaque mode d'échec vers l'exception de domaine correspondante, déclaration de capacités bien formée, et — pour chaque capacité déclarée absente — conformité au cas de la taxonomie retenu pour ce couple. Elle s'exécute sur enregistrements rejoués en CI, et en mode réel à la demande. **Ajouter un fournisseur, c'est écrire un adaptateur et faire passer cette suite** : un travail borné, qui ne peut pas régresser sur les quatre autres. Sans ce garde-fou, cinq adaptateurs seraient imprudents pour un projet solo.

**Les abonnements grand public ne sont pas une option d'exécution.** Claude Pro/Max, ChatGPT Plus et Gemini Advanced sont des licences d'usage **personnel**, sans API stable ni contractuelle. Une application qui sert des utilisateurs ne peut s'y adosser : usage hors licence, surface non documentée susceptible de casser sans préavis, aucun engagement de disponibilité. Seuls des accès API facturés à l'usage ou un modèle auto-hébergé sont des fournisseurs légitimes à l'exécution.

En revanche, ces abonnements sont parfaitement légitimes pour **développer** Chaudron — écrire du code, concevoir des prompts, explorer des formats de sortie. La distinction porte sur le poste de dépense, pas sur l'outil : un abonnement peut construire le produit, il ne peut pas le servir.

## Conséquences

### Positives

- Le produit exploite pleinement les fournisseurs capables au lieu de s'aligner sur le plus faible : sortie structurée stricte et mise en cache de prompt sont utilisées là où elles existent.
- Le foyer choisit selon ses propres critères — coût, capacités, juridiction — sans que Chaudron impose un fournisseur.
- La logique métier est testable sans réseau : un `FakeRecipeSuggester` en mémoire suffit, et la majorité des tests n'a jamais besoin d'un vrai fournisseur.
- La taxonomie de dégradation rend le comportement en capacité manquante prévisible et revuable : pour chaque couple, on sait quel cas s'applique et pourquoi.
- Les tests de conformité bornent le coût d'ajout d'un fournisseur et transforment une régression potentielle en échec de CI.
- Les erreurs de fournisseur sont traduites une fois, au bon endroit, au lieu de fuir en `except AnthropicError` dispersés dans les routes.

### Négatives

- **La matrice de test et de maintenance est importante, et c'est le vrai prix de la décision.** Cinq adaptateurs × chaque fonctionnalité de modèle × chaque capacité consommée : chaque nouvelle fonctionnalité multiplie les cas à trancher, implémenter, exposer et tester. Pour un développeur solo, c'est une charge structurelle, pas un coût ponctuel. Les tests de conformité la rendent tenable — ils ne la suppriment pas.
- **Cinq SDK à suivre.** Chacun a son rythme de publication, ses ruptures d'API et ses modèles dépréciés. Une mise à jour de dépendance peut casser un adaptateur sans toucher aux autres, et il faut le détecter avant l'utilisateur.
- **Les capacités statiques sont une table à maintenir à la main.** Chaque nouveau modèle publié par l'un des quatre fournisseurs demande une entrée ; une table périmée fait déclarer une capacité absente ou promet une capacité inexistante.
- **La déclaration sondée d'Ollama est une source de bugs propre.** Elle dépend d'une instance tierce joignable au moment de la configuration ; l'utilisateur peut changer de modèle après coup sans que Chaudron le sache, laissant des capacités périmées. Il faut gérer l'instance injoignable, la donnée obsolète et une voie de rafraîchissement — trois chemins d'erreur qui n'existent pour aucun autre adaptateur.
- **Le chemin d'émulation a ses propres modes d'échec** : latences variables et échecs résiduels que l'interface doit présenter honnêtement plutôt que masquer.
- **L'indicateur de mode dégradé est du travail d'UI récurrent** : chaque capacité manquante doit être expliquée en langue naturelle, avec une remédiation actionnable. Un indicateur vague est pire qu'aucun.
- **Support utilisateur plus difficile.** « Ça ne marche pas » peut venir de leur instance Ollama, de leur quota, d'une clé d'abonnement collée à la place d'une clé d'API, du modèle qu'ils ont choisi, ou du code. Le diagnostic exige de faire remonter le fournisseur, les capacités détectées et le mode d'échec dans les erreurs présentées.

## Alternatives écartées

- **Appels directs au SDK dans les handlers** — le moins de code aujourd'hui. Écarté : cinq fournisseurs sélectionnés par foyer à l'exécution se traduiraient par des branchements conditionnels dans chaque handler.
- **Interface au plus petit dénominateur commun** — une seule surface, celle que tous les fournisseurs savent honorer, donc aucune matrice de dégradation à maintenir. Écarté explicitement : cela aligne le produit sur le fournisseur le plus faible et prive la majorité des foyers des capacités qu'ils paient.
- **Un seul fournisseur de premier rang en v1, les autres plus tard** — diviserait la matrice par quatre immédiatement. Écarté : le choix du fournisseur est un critère d'adoption (coût, juridiction, compte déjà existant), et l'ajout tardif d'un adaptateur sans suite de conformité préexistante est bien plus risqué que sa construction initiale.
- **Une passerelle multi-fournisseurs (LiteLLM, OpenRouter)** — normalise les fournisseurs sans écrire d'adaptateurs. Écarté : une passerelle logicielle impose son modèle de données et suit mal les capacités spécifiques — exactement ce que la décision cherche à exploiter ; une passerelle hébergée ajoute un intermédiaire qui voit toutes les requêtes, ce qui détruit l'argument de souveraineté et contredit le modèle par foyer de l'ADR-0007.
- **Abstraction générique `LLMClient` (`complete(prompt) -> str`)** — une seule interface pour tout. Écartée : elle place la construction du prompt et le parsing de la réponse dans le domaine, et ne permet aucune déclaration de capacités.
- **Abonnement grand public exploité via une automatisation de navigateur** — supprimerait le coût à l'usage. Écarté : usage hors licence, surface non documentée, aucune garantie de disponibilité.

## Révision

- Si la matrice capacité × fonctionnalité devient ingérable à la main, formaliser les couples dans une table de décision unique, vérifiée par les tests de conformité, plutôt que dispersée entre les adaptateurs et l'interface.
- Retirer un adaptateur si les mesures montrent qu'aucun foyer ne l'utilise : chaque adaptateur conservé a un coût de maintenance permanent, et cinq est un plafond, pas un point de départ.
- Réévaluer la détection de capacités d'Ollama si l'instance expose un moyen fiable de signaler un changement de modèle.
- Réévaluer la granularité des ports si un troisième cas d'usage de modèle apparaît (par exemple la normalisation de libellés produits).
