# 0004. PWA installable plutôt qu'application mobile native

## Statut

Accepté — 2026-08-03

## Contexte

Chaudron est une application d'usage mobile : on saisit son stock devant un placard, on scanne un code-barres en rangeant les courses, on photographie un ticket en sortant du magasin. Le desktop est secondaire.

Le déclencheur du choix est **économique et assumé** : publier une application native impose un compte développeur Apple à 99 $/an, reconduit annuellement, et un compte Google Play à 25 $ (paiement unique). Pour un projet en phase 1 familiale, sans revenu et sans certitude d'atteindre la phase 2, c'est un abonnement récurrent engagé avant la première ligne de valeur — et sur iOS, une dépense qui, non renouvelée, retire l'application des appareils des utilisateurs à l'expiration.

Le deuxième facteur est la charge de maintenance : une application native, c'est une codebase supplémentaire par plateforme (ou un framework cross-platform et sa propre dette), un cycle de revue par les stores à chaque correctif, et une matrice de versions à supporter. Sur un projet solo, ce coût se paie sur le temps de développement fonctionnel.

## Décision

Chaudron est une PWA installable : React + Vite, manifeste web, service worker, codebase frontend séparée du backend.

Les capacités nécessaires sont obtenues via les API web standard : `BarcodeDetector` avec repli WASM (ZXing) sur les navigateurs qui ne l'exposent pas, `getUserMedia` pour le flux caméra, `<input type="file" capture>` pour la photo de ticket, service worker pour le cache d'application et la consultation hors ligne du stock.

Aucun compte développeur n'est ouvert, aucune application n'est soumise à un store.

## Conséquences

### Positives

- Zéro coût de distribution récurrent, zéro délai de revue : un correctif déployé est disponible au rechargement suivant.
- Une seule codebase frontend, un seul pipeline de build.
- L'installation se fait par URL — pratique pour un usage familial, où on partage un lien.
- Aucune dépendance à la politique d'un store : pas de risque de retrait, pas de commission, pas de règle à interpréter.

### Négatives

Ces limitations sont réelles et pénalisent le produit ; elles ne sont pas des détails à contourner.

- **Notifications push sur iOS : praticables mais fragiles.** Depuis iOS 16.4, la Web Push existe, mais *uniquement* si l'utilisateur a ajouté la PWA à l'écran d'accueil — un geste que la plupart des gens ne feront pas. Or les alertes de péremption sont précisément la fonctionnalité qui fait revenir l'utilisateur. Sur iOS, on doit assumer qu'une partie significative des utilisateurs ne les recevra jamais, et prévoir un canal de repli (e-mail, ou une vue « à consommer bientôt » consultée activement).
- **Accès caméra dégradé.** `getUserMedia` fonctionne, mais uniquement en contexte sécurisé (HTTPS), et le contrôle fin (autofocus, torche, zoom optique) est inégal selon les navigateurs. `BarcodeDetector` n'est pas disponible sur Safari : il faut embarquer un décodeur WASM, ce qui alourdit le bundle et donne un scan plus lent et moins tolérant aux codes abîmés que l'API native d'un SDK mobile.
- **Installabilité opaque.** Sur Android, une invite d'installation est proposée. Sur iOS, il n'y en a aucune : l'utilisateur doit passer par Partager → « Sur l'écran d'accueil ». Il faut expliquer ce geste dans l'interface, et une partie des utilisateurs ne le fera pas — ils resteront dans un onglet Safari, sans push et avec un stockage susceptible d'être purgé après plusieurs semaines d'inactivité.
- **Découvrabilité nulle.** Personne ne trouve Chaudron en cherchant « gestion de stock alimentaire » dans l'App Store. La distribution repose entièrement sur le partage direct et le référencement web. En phase 1 c'est sans conséquence ; en phase 2 c'est un handicap d'acquisition majeur.
- Pas de widget d'écran d'accueil, pas de partage natif entrant riche, pas d'intégration à l'assistant vocal du système.
- Le stockage local (IndexedDB, cache du service worker) peut être évincé par le système : le mode hors ligne est un confort, pas une garantie.

## Alternatives écartées

- **Natif iOS + Android (Swift + Kotlin)** — la meilleure expérience possible : caméra pilotée finement, push fiable, présence en store. Écarté : deux codebases pour un développeur solo, plus les coûts de compte développeur, avant toute validation du produit.
- **React Native ou Flutter** — une codebase pour deux plateformes, accès natif à la caméra et au push. Écarté : ne supprime **pas** le coût des comptes développeur ni les cycles de revue, qui sont le déclencheur de la décision. Ajoute un framework et sa chaîne de build native à maintenir.
- **Capacitor (PWA empaquetée en application native)** — réutilise la codebase web et donne accès au push natif et aux stores. C'est l'alternative la plus sérieuse, et le chemin de migration privilégié. Écarté **pour maintenant** : elle rétablit exactement les coûts que la décision cherche à éviter (comptes développeur, soumissions, revues), sans être requise en phase 1. À reprendre lorsque les signaux de révision ci-dessous se déclenchent.
- **Application web classique, non installable** — plus simple encore. Écartée : renoncer au manifeste et au service worker retire l'installation, le mode hors ligne et toute possibilité de push, sans économiser grand-chose.

## Révision

Reconsidérer, en visant Capacitor plutôt qu'un développement natif *ab initio* — la codebase React est réutilisée :

- **Signal principal** : si les mesures montrent que les alertes de péremption ne sont pas reçues par une part significative des utilisateurs iOS, et que ce défaut se traduit par une baisse d'usage. C'est le point de rupture le plus probable, puisqu'il touche la boucle de rétention.
- Si le taux d'installation sur écran d'accueil reste faible (< 30 % des utilisateurs actifs) malgré des instructions explicites dans l'interface.
- Si la qualité du scan de code-barres via WASM s'avère un motif d'abandon récurrent en usage réel.
- Si la phase 2 démarre effectivement et que l'acquisition par les stores devient nécessaire à la croissance. Le coût annuel devient alors défendable, puisqu'il s'adosse à un usage réel.
