# Note technique de faisabilité — scan de code-barres et résolution EAN

**Projet :** Pantry (PWA React + Vite / backend FastAPI) — gestion de stock alimentaire domestique
**Date :** 3 août 2026
**Statut :** note de cadrage, à relire avant de figer l'architecture du module « scan »

---

## Méthode et niveau de confiance

Cette note distingue trois niveaux :

| Marqueur | Signification |
|---|---|
| **[V]** | Vérifié le 3 août 2026 par requête directe (curl / registre npm / API GitHub) ou lecture de la source primaire (spec, doc officielle, bug tracker). La commande ou l'URL est donnée. |
| **[S]** | Sourcé sur une page tierce crédible mais non reproduit en propre. |
| **[NV]** | **Non vérifié.** Affirmation plausible mais que je n'ai pas pu confirmer — typiquement parce qu'elle exige un appareil physique, un compte payant, ou qu'elle a été bloquée par un rate limit. Traiter comme une hypothèse à tester. |

Les mesures locales (tailles de fichiers, comptages OFF) ont été faites depuis le poste de dev le 3 août 2026 ; elles bougeront.

---

## 1. Lecture de code-barres dans le navigateur

### 1.1 L'API native `BarcodeDetector` : l'état réel

La conclusion tient en une phrase : **`BarcodeDetector` est inutilisable comme socle unique.** Elle n'est disponible ni sur iOS, ni sur Firefox, ni sur Chrome/Windows, ni sur Chrome/Linux.

Données de compatibilité, extraites de la source primaire (`mdn/browser-compat-data`, fichier `api/BarcodeDetector.json`) **[V]** :

| Navigateur | Support | Restriction |
|---|---|---|
| Chrome desktop | 88+ | **ChromeOS et macOS uniquement** (83–87 : macOS seul) |
| Chrome Android | 83+ | OK — cible principale |
| Edge | 83+ | **macOS uniquement** |
| Opera | 69+ | **macOS uniquement** |
| Firefox / Firefox Android | ❌ | `version_added: false` |
| Safari (macOS) | 17+ | **derrière un feature flag** (« Shape Detection API ») |
| Safari iOS | 17+ | idem — flag, et voir ci-dessous |
| Samsung Internet | 83+ | aligné sur Chrome Android |
| WebView Android | 83+ | aligné sur Chrome Android |

Source : <https://github.com/mdn/browser-compat-data/blob/main/api/BarcodeDetector.json>

La documentation Chrome le confirme en toutes lettres : *« Barcode detection is available on macOS, ChromeOS, and Android »*, et *« Google Play Services are required on Android »* — l'implémentation délègue aux bibliothèques de l'OS, elle n'embarque pas son propre décodeur **[V]** (<https://developer.chrome.com/docs/capabilities/shape-detection>).

MDN classe l'API en **« Limited availability »** et **« Experimental »**, avec exigence de contexte sécurisé (HTTPS) **[V]** (<https://developer.mozilla.org/en-US/docs/Web/API/BarcodeDetector>).

caniuse donne 76,36 % d'usage global « support ou support partiel », mais ce chiffre est trompeur : il agrège les « partial support » de Chrome desktop, qui sont en réalité des non-support sur Windows et Linux **[V]** (<https://caniuse.com/mdn-api_barcodedetector>).

#### Le cas iOS : le flag existe, il ne sert à rien

Point le plus important de cette section. Sur iOS, le flag « Shape Detection API » est bien présent dans Réglages > Safari > Avancé > Feature Flags, **mais l'activer ne rend pas la détection fonctionnelle**. Le bug WebKit **#281848** (« Shape Detection API doesn't work on iOS ») est ouvert depuis le 21 octobre 2024 et **toujours au statut NEW** ; les commentaires signalent l'échec sur Safari 17.6.x, 18.3, 18.4, 18.5, puis sur les bêtas iOS 26 (juin 2025), le dernier commentaire datant de juillet 2026 **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=281848>).

Rien dans les notes de version WebKit de Safari 26.0 à 26.6 n'annonce l'activation par défaut de Shape Detection **[S]** (<https://webkit.org/blog/17333/webkit-features-in-safari-26-0/>, <https://webkit.org/blog/18178/webkit-features-for-safari-26-6/>).

> **Conséquence de conception :** ne pas écrire de code qui suppose `BarcodeDetector` présent. Et surtout, ne pas se contenter d'un `if ('BarcodeDetector' in window)` : sur macOS, l'objet peut exister sans être fiable. Le test de disponibilité doit être `await BarcodeDetector.getSupportedFormats()` et vérifier que `ean_13` en fait partie.

#### Piège pour la boucle de dev locale

Le poste de développement tourne sous Linux. **`BarcodeDetector` n'y existe dans aucun navigateur** (Chrome : macOS/ChromeOS/Android seulement ; Firefox : jamais). Le chemin « natif » ne sera donc **jamais exercé en dev local** — uniquement sur un téléphone Android réel. C'est exactement le genre de branche qui pourrit sans qu'on s'en aperçoive. Argument de plus pour ne pas maintenir deux chemins de code.

### 1.2 Bibliothèques de repli — comparatif

Toutes les métadonnées ci-dessous ont été relevées **le 3 août 2026** sur `registry.npmjs.org` et l'API GitHub **[V]**.

| Paquet | Version / date | Licence | Deps | Repo (⭐ / dernier push / issues) | Nature |
|---|---|---|---|---|---|
| **`zxing-wasm`** | 3.1.2 — 2026-07-18 | MIT | `@types/emscripten`, `type-fest` | Sec-ant/zxing-wasm — 246 ⭐ / 2026-08-01 / 9 | ZXing-C++ compilé en WASM |
| **`barcode-detector`** | 3.2.1 — 2026-07-12 | MIT | `zxing-wasm` | Sec-ant/barcode-detector — 227 ⭐ / 2026-08-03 | Poly/ponyfill de l'API standard, adossé à `zxing-wasm` |
| **`@zxing/library`** | 0.23.0 — 2026-04-29 | Apache-2.0 | `ts-custom-error` | zxing-js/library — 2 923 ⭐ / 2026-07-25 / **170 issues** | Port TypeScript pur de ZXing |
| **`html5-qrcode`** | 2.3.8 — **2023-04-15** | Apache-2.0 | aucune | mebjas/html5-qrcode — 6 191 ⭐ / 2025-12-01 / **441 issues** | Composant UI complet, embarque `@zxing/library` |
| **`@ericblade/quagga2`** | 1.12.1 — 2025-12-20 | MIT | `gl-matrix` | ericblade/quagga2 — 908 ⭐ / 2026-07-25 | Décodeur 1D en JS pur |

#### Maintenance

- **`zxing-wasm` / `barcode-detector`** : cadence soutenue et régulière. Pour `zxing-wasm` : 3.0.1 (2026-03-09), 3.0.2 (04-01), 3.0.3 (05-04), 3.1.0 (06-01), 3.1.1 (07-12), 3.1.2 (07-18) **[V]**. Même mainteneur (Sec-ant) pour les deux, ce qui est à la fois une garantie de cohérence et un **risque de bus factor = 1** — à noter au registre des risques.
- **`@zxing/library`** : à surveiller. Historique des publications : 0.21.3 le **2024-08-21**, puis plus rien jusqu'à 0.22.0 le **2026-04-27** **[V]** — soit **20 mois sans release**. Le projet est reparti, mais 170 issues ouvertes sur un port JS manuel de ZXing, ça veut dire des divergences accumulées avec l'amont C++.
- **`html5-qrcode`** : **dernière publication npm le 15 avril 2023**, soit plus de trois ans **[V]**. Le README annonce explicitement le mode maintenance (*« the author shall not be able to make any bug fixes or improvements for the time-being. Pull requests also won't be merged »*) et 441 issues sont ouvertes **[S]** (<https://github.com/mebjas/html5-qrcode>). **À écarter.** C'est d'autant plus vrai qu'il embarque `@zxing/library` : on hériterait d'une version figée en 2023 d'une dépendance déjà en retard.
- **`@ericblade/quagga2`** : vivant, mais **1D uniquement** et décodeur JS pur. Pertinent seulement si on veut zéro WASM.

#### Taille de bundle — mesures réelles

L'archive `zxing-wasm@3.1.2` fait 3,77 Mo décompressée, mais ce chiffre est un épouvantail : elle contient **trois binaires WASM alternatifs** dont on n'en charge qu'un **[V]**.

| Artefact | Brut | gzip -9 (mesuré) |
|---|---|---|
| `dist/reader/zxing_reader.wasm` | 1 065 866 o | **448 787 o** |
| `dist/full/zxing_full.wasm` (lecture + écriture) | 1 511 909 o | — |
| `dist/writer/zxing_writer.wasm` | 648 328 o | — |
| `dist/es/reader/index.js` (colle JS) | 42 595 o | — |

Donc : **~450 Ko gzip pour le décodeur lecture seule**, à charger une fois puis à mettre en cache dans le service worker. Brotli ferait sensiblement mieux — non mesuré, `brotli` n'est pas installé sur le poste **[NV]**.

C'est un coût réel mais acceptable pour une PWA d'inventaire : le WASM n'est chargé **qu'à l'ouverture de l'écran de scan**, pas au démarrage de l'app, et il est ensuite servi depuis le cache y compris hors ligne.

`barcode-detector@3.2.1` n'ajoute que 260 Ko décompressés de colle JS, le WASM venant de `zxing-wasm` **[V]**.

#### Formats supportés

`zxing-wasm` / `barcode-detector` couvrent largement au-delà du besoin. Pour le commerce de détail, sont lisibles : `EAN13`, `EAN8`, `UPCA`, `UPCE`, `ISBN`, ainsi que toute la famille `DataBar` (Omni, Stacked, Limited, Expanded) — cette dernière compte, on la trouve sur les petits conditionnements et les produits frais **[V]** (README de `zxing-wasm@3.1.2`). À noter : `EAN5` et `EAN2` (les add-ons) sont en écriture seule, pas en lecture.

L'API native Chrome, elle, expose 13 formats (`aztec`, `code_128`, `code_39`, `code_93`, `codabar`, `data_matrix`, `ean_13`, `ean_8`, `itf`, `pdf417`, `qr_code`, `upc_a`, `upc_e`) — **pas de DataBar** **[V]**. Le repli WASM est donc, sur ce point précis, *plus capable* que le natif.

#### Performance mobile

Je n'ai **pas** pu mesurer le débit de décodage sur téléphone réel **[NV]** — cela demande un appareil et un protocole de test. Ce qu'on peut affirmer :

- ZXing-C++ compilé en WASM est structurellement plus rapide que le port JS `@zxing/library`, qui réimplémente le même algorithme dans un langage plus lent et sans SIMD.
- L'API native déléguant au décodeur de l'OS (voire au silicium du module caméra selon la doc Chrome **[V]**), elle reste la plus rapide là où elle existe.
- Le vrai levier de perf en pratique n'est pas le décodeur mais **la boucle d'acquisition** : décoder à ~10 fps sur une région d'intérêt réduite plutôt qu'à 60 fps sur l'image pleine, et faire tourner le décodage dans un **Web Worker** pour ne pas bloquer le thread principal (`zxing-wasm` fonctionne en worker, l'API `BarcodeDetector` est également exposée aux Web Workers selon MDN **[V]**).

#### Alternatives commerciales

STRICH, Scandit, Scanbot, Dynamsoft proposent des SDK web propriétaires réputés plus robustes sur codes abîmés et éclairage difficile. **Je n'ai pas vérifié leurs tarifs** **[NV]**. À garder en plan B *uniquement* si les tests terrain montrent un taux d'échec de lecture rédhibitoire — pour un projet domestique, le coût de licence est vraisemblablement disqualifiant.

### 1.3 Recommandation

> **Utiliser `barcode-detector` (le ponyfill de Sec-ant), pas `zxing-wasm` directement, et pas de branche « natif si disponible ».**

Justification :

1. **Une seule API, un seul chemin de code.** Le ponyfill expose exactement l'interface standard `BarcodeDetector`. Le jour où Safari répare #281848 et où Chrome/Linux s'aligne, on passe du ponyfill au polyfill (ou on supprime l'import) sans toucher au code applicatif.
2. **Un seul comportement à tester.** Voir le piège de la §1.1 : la branche « natif » ne serait jamais exercée en dev local. Deux chemins dont un jamais testé, c'est un chemin cassé qui s'ignore. Le gain de perf du natif ne justifie pas ce risque sur une app d'inventaire domestique où l'on scanne quelques dizaines d'articles par semaine.
3. **Meilleure couverture de formats** que le natif (DataBar).
4. **Licence MIT**, compatible avec tout ; maintenance active et fréquente.
5. **Fonctionne hors ligne** une fois le WASM précaché — contrairement à toute solution serveur.

Import : `import { BarcodeDetector } from "barcode-detector/ponyfill"`, restreint aux formats utiles :

```ts
const detector = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e", "databar", "databar_expanded"] });
```

Restreindre les formats n'est pas cosmétique : ça réduit le travail par image et surtout **le taux de faux positifs**.

---

## 2. Accès caméra en PWA

### 2.1 `getUserMedia` — contraintes

- **HTTPS obligatoire.** `navigator.mediaDevices` n'est exposé qu'en contexte sécurisé ; `http://localhost` est traité comme sécurisé pour le dev **[V]** (<https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia>). En pratique : le dev sur un téléphone via l'IP du LAN (`http://192.168.x.x:5173`) **ne marchera pas**. Il faut soit un tunnel HTTPS, soit un certificat local, soit tester via déploiement. À prévoir dans la boucle de dev dès le départ, ça bloque tôt.
- **Caméra arrière** : `video: { facingMode: { ideal: "environment" } }`. Utiliser `ideal` et non `exact` — avec `exact`, l'appel échoue purement et simplement sur les appareils sans caméra arrière (webcams de bureau), ce qui casse le dev desktop.
- **Piège Android multi-caméras** : `facingMode: "environment"` ne garantit pas de tomber sur le *bon* capteur arrière. Sur les téléphones à trois objectifs, le navigateur peut sélectionner l'ultra-grand-angle, qui n'a souvent pas de mise au point rapprochée — résultat : un code-barres à 10 cm reste flou et ne décode jamais. Repli : `enumerateDevices()` après autorisation, puis laisser l'utilisateur choisir la caméra, avec mémorisation du choix. **[NV]** sur la fréquence réelle du problème, mais le mécanisme est certain.
- **Résolution** : demander `width: { ideal: 1280 }` au minimum. Un EAN-13 comporte des barres de 1 à 4 modules ; pour un décodage fiable il faut au moins 2 px par module, ce qui implique un code occupant environ 190 px de large dans l'image **[S]** (<https://www.scandit.com/blog/make-barcode-scanner-app-performant/>). Sur un flux 640×480 avec un code occupant un tiers de la largeur, on est en dessous. 1280×720 est un bon compromis charge CPU / lisibilité.
- **Mise au point, torche, zoom** : Chrome Android expose `focusMode`, `focusDistance`, `torch` et `zoom` via `track.getCapabilities()` puis `applyConstraints()`. **Safari iOS n'expose pas ces contraintes** **[S]** (<https://www.dynamsoft.com/codepool/camera-focus-control-on-web.html>). Non vérifié sur appareil **[NV]**. Conséquence concrète : **pas de bouton torche sur iPhone**, alors que c'est le remède numéro un aux échecs de lecture en lumière basse. À intégrer dans l'UX : le bouton torche doit être conditionné à `"torch" in track.getCapabilities()` et simplement absent sinon, pas grisé.
- Toujours `track.stop()` sur toutes les pistes en quittant l'écran de scan. Un flux laissé ouvert garde la LED allumée, vide la batterie, et sur iOS contribue aux blocages décrits ci-dessous.

### 2.2 Le cas iOS/Safari — le point qui décide

**Réponse courte : oui, la caméra fonctionne dans une PWA installée sur l'écran d'accueil, depuis iOS 13.4 (mars 2020). Mais la fiabilité reste discutable en 2026, et c'est le principal risque du projet.**

Le détail, sourcé sur le bug tracker WebKit :

**a) Le support de base existe.** Le bug **#185448** (« getUserMedia not working in apps added to home screen that run in standalone mode ») est **RESOLVED FIXED**. La correction est confirmée dans iOS 13.4 bêta 1 (février 2020), livrée publiquement en mars 2020 **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=185448>). Le plancher de version est donc iOS 13.4 — non contraignant en 2026.

**b) Mais les permissions ne persistent pas.** Le bug **#215884** porte sur les redemandes d'autorisation caméra en mode standalone. Il est marqué RESOLVED/CONFIGURATION CHANGED, **mais le fil continue de recevoir des rapports** : amélioration partielle en iOS 14.5 bêta (l'autorisation ne se réinitialise plus à chaque page, mais ne survit pas à la fermeture de l'app), et des commentaires jusqu'en **janvier 2026** signalant que le problème persiste sur iOS 18.5+ **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=215884>). Deux symptômes distincts et tous deux gênants :
   - l'autorisation accordée **dans Safari** ne se transmet **pas** à la PWA installée ;
   - l'autorisation est **redemandée après chaque redémarrage** de la PWA.

**c) Et le flux vidéo peut être noir.** Le bug **#252465** décrit un `getUserMedia()` qui rend un `<video>` noir ou vide en mode PWA, alors que le même code fonctionne dans Safari. Marqué RESOLVED FIXED, mais avec des régressions signalées de façon récurrente sur iOS 18.0.1, 18.1, 18.4.1 et 18.5 jusqu'en juin 2025 **[V]** (<https://bugs.webkit.org/show_bug.cgi?id=252465>).

**d) Ce que recommande l'écosystème.** La base de connaissances de STRICH (éditeur d'un SDK de scan web, donc bien placé) recommande : vérifier qu'on est sur la dernière version d'iOS et redémarrer le téléphone ; utiliser l'app dans Safari plutôt qu'installée ; ou **retirer la balise `apple-mobile-web-app-capable`** pour forcer l'exécution dans Safari tout en gardant l'icône sur l'écran d'accueil **[V]** (<https://kb.strich.io/article/29-camera-access-issues-in-ios-pwa>).

Ce dernier conseil est un arbitrage à faire consciemment : retirer `apple-mobile-web-app-capable`, c'est **renoncer au mode standalone** (barre Safari visible, pas de plein écran) en échange d'un accès caméra fiable. Pour une app d'inventaire domestique, l'esthétique plein écran vaut probablement moins que « le scan marche ». À garder comme **interrupteur de secours**, pas comme choix par défaut.

**e) Ce que je n'ai pas pu vérifier.** **[NV]** Le comportement réel sur **iOS 26 avec un iPhone physique** en août 2026. Les bugs ci-dessus ont des historiques d'aller-retour ; il est possible que la situation se soit améliorée depuis les derniers commentaires publics. Il est également possible qu'elle ait régressé.

> **Action bloquante recommandée :** avant d'investir dans le module de scan, faire un **prototype jetable** — une page HTML servie en HTTPS, `getUserMedia` + `barcode-detector`, installée en PWA sur un iPhone réel à jour. Vérifier : (1) le flux vidéo s'affiche, (2) un EAN-13 se décode, (3) l'autorisation survit à un kill + relance de l'app. Une demi-journée. Si (3) échoue, ce n'est pas rédhibitoire, mais l'UX doit être conçue autour de cette contrainte dès le départ plutôt qu'après coup.

### 2.3 Comportement hors ligne

**Oui, on peut scanner sans réseau — sous conditions.**

| Étape | Hors ligne ? |
|---|---|
| Ouvrir la caméra (`getUserMedia`) | ✅ purement local |
| Décoder l'EAN (WASM) | ✅ si le `.wasm` est précaché par le service worker |
| Résoudre l'EAN → fiche produit | ❌ sauf si en cache local |
| Enregistrer l'ajout au stock | ✅ si écrit en IndexedDB puis synchronisé |

Points d'attention :

- **Précacher explicitement le `.wasm`.** Il est chargé dynamiquement par la colle JS, pas via un `import` statique : un service worker généré automatiquement (`vite-plugin-pwa` / Workbox) risque de **ne pas le voir**. Il faut l'ajouter à la liste de précache à la main et vérifier le manifeste généré. Erreur classique, et elle ne se manifeste qu'en avion.
- **Pas de Background Sync sur iOS.** L'API Background Synchronization n'est pas supportée par Safari et il n'y a pas d'indication qu'elle le soit prochainement **[S]** (<https://caniuse.com/background-sync>). Il ne faut donc **pas** bâtir la synchronisation dessus. Le modèle qui marche partout : file d'attente d'opérations en IndexedDB, vidée sur l'événement `online`, au retour au premier plan (`visibilitychange`), et au démarrage de l'app.
- **Conception offline-first assumée.** Le scan produit un EAN. Le stock est modifié **immédiatement en local** avec la fiche produit en attente ; la résolution OFF est une opération asynchrone qui enrichit l'entrée plus tard. Ce n'est pas une dégradation gracieuse, c'est le mode nominal — cela rend l'app agréable même avec du réseau, puisque le fond de placard capte rarement bien.
- **[NV]** Le quota de stockage et la politique d'éviction pour une PWA installée sur iOS en 2026. Prudence : ne pas considérer IndexedDB comme un stockage durable, prévoir une synchronisation serveur rapide et un export.

---

## 3. Base de données produits — Open Food Facts

### 3.1 L'API

Vérifié par requêtes directes le 3 août 2026 **[V]**.

**Versions.** v3 (dernière sous-version **v3.6**) est la version courante recommandée. **v2 est explicitement marquée dépréciée** dans la doc officielle, encore supportée pour compatibilité **[V]** (<https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/index.md>). → **Développer en v3 dès le départ.**

**Endpoint de lookup :**

```
GET https://world.openfoodfacts.org/api/v3/product/{barcode}.json?fields=…
```

Réponse pour un produit existant (Nutella, `3017624010701`) :

```json
{"code":"3017624010701","errors":[],
 "product":{"brands":"Ferrero","code":"3017624010701","product_name":"Nutella"},
 "result":{"id":"product_found","lc_name":"Product found","name":"Product found"},
 "status":"success","warnings":[]}
```

Réponse pour un code absent (`3760091721234`) — **HTTP 404** :

```json
{"code":"3760091721234",
 "errors":[{"field":{"id":"code","value":"3760091721234"},
            "impact":{"id":"failure"},
            "message":{"id":"product_not_found"}}],
 "result":{"id":"product_not_found","name":"Product not found"},
 "status":"failure","warnings":[]}
```

> Le contrat d'erreur v3 est **structuré** (`result.id`, `errors[]`) et diffère de v2 (`status: 0` / `status_verbose`). Se brancher sur `result.id === "product_not_found"` et sur le code HTTP, jamais sur une chaîne libre.

**Champs utiles** (testés, non vides sur un produit réel) :

| Champ | Contenu |
|---|---|
| `product_name`, `product_name_fr` | nom ; toujours demander la variante `_fr` |
| `brands` | marque, chaîne libre séparée par des virgules |
| `quantity` | contenance, **texte libre** (`"400.0 g"`) — à parser, jamais un nombre |
| `categories_tags` | taxonomie, préfixée par langue : `["en:spreads","fr:pates-a-tartiner","de:Other"]` — le mélange de langues est normal |
| `nutriscore_grade` | `a`…`e` |
| `nova_group` | 1–4 (degré d'ultra-transformation) |
| `allergens_tags` | `["en:nuts"]` |
| `image_front_small_url` | vignette (voir §3.2 pour la licence) |
| `ecoscore_grade` | encore servi, mais la doc parle désormais de **Green-Score** — renommage en cours, à ne pas traiter comme stable |
| `serving_size` | portion |

**Le paramètre `fields=` est indispensable** : une fiche complète pèse plusieurs centaines de kilo-octets. Demander 10 champs ramène quelques centaines d'octets. Réduit la bande passante, le temps de réponse, et la charge sur l'infrastructure OFF.

**CORS :** l'API renvoie `access-control-allow-origin: *` **[V]** (relevé dans les en-têtes). Le frontend *pourrait* donc appeler OFF directement. **Ne pas le faire** — voir §3.5, la raison est architecturale et non technique.

**Environnement de staging :** `https://world.openfoodfacts.net`, protégé par Basic Auth `off` / `off`. La doc demande explicitement que **tous les appels de développement passent par le staging** **[V]**.

### 3.2 Conditions d'usage — licences, attribution, rate limits

Toutes ces obligations sont dans la doc officielle **[V]** (<https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/index.md>) et sur <https://world.openfoodfacts.org/data>.

**Licences — et elles diffèrent, c'est le piège :**

| Élément | Licence |
|---|---|
| La base (structure) | **ODbL 1.0** — <https://opendatacommons.org/licenses/odbl/1.0/> |
| Les contenus individuels | **DbCL 1.0** — <https://opendatacommons.org/licenses/dbcl/1.0/> |
| **Les images produits** | **CC BY-SA 3.0** — <https://creativecommons.org/licenses/by-sa/3.0/> |

La doc ajoute un avertissement à propos des images : *« They may contain graphical elements subject to copyright or other rights »* — les emballages photographiés contiennent des logos et visuels de marque qui restent la propriété de leurs titulaires. Afficher une vignette dans une app privée d'inventaire domestique est sans enjeu ; republier ces images le serait davantage.

**ODbL = attribution + share-alike.** Concrètement pour Pantry :
- afficher une attribution « Données produits : Open Food Facts — ODbL » quelque part dans l'UI ;
- le share-alike mord **si l'on combine la base OFF avec une autre base** : la base dérivée devrait alors être publiée en open data. Un cache de fiches produits juxtaposé à un stock personnel est un cas limite ; pour un usage privé non redistribué, la question ne se pose pas en pratique. **Elle se poserait si Pantry devenait un service multi-utilisateurs public.** À trancher avant, pas après.

**User-Agent obligatoire.** *« We ask you to always use a custom User-Agent to identify your app »*, au format `AppName/Version (ContactEmail)` **[V]**. Les lectures ne demandent **aucune autre authentification** ; les écritures (édition de fiche, upload de photo) exigent un compte.

**Rate limits — citation exacte [V] :**

- **15 req/min/IP** pour toutes les lectures produit (`GET /api/v*/product` ou page produit) ;
- **10 req/min/IP** pour les recherches (`GET /api/v*/search`) — *« don't use it for a search-as-you-type feature, you would be blocked very quickly »* ;
- **aucune limite** sur les écritures ;
- limites globales additionnelles indépendantes de l'IP → **HTTP 503** ;
- dépassement = **bannissement d'IP possible** (réversible par mail à `reuse@openfoodfacts.org`).

J'ai touché ce mur pendant la rédaction de cette note : après quelques requêtes de recherche, l'API a renvoyé une page HTML « Page temporarily unavailable ». **La limite n'est pas théorique, et elle ne renvoie pas toujours du JSON** — le client HTTP doit gérer une réponse HTML inattendue sans planter.

**Formulaire de déclaration.** La doc demande de remplir un formulaire d'usage de l'API pour que l'équipe identifie les réutilisations et évite les bannissements accidentels **[V]**. Cinq minutes, à faire.

**Avertissement sur la qualité.** *« Data […] is provided voluntarily by users […] there are no assurances that the data is accurate, complete, or reliable. The user assumes the entire risk of using the data. »* **[V]** Voir §4.

### 3.3 Couverture réelle sur les produits français

Mesuré en direct le 3 août 2026 via `GET /api/v2/search` **[V]** :

| Indicateur | Valeur |
|---|---|
| Produits en base, tous pays | **4 663 574** |
| Produits déclarés vendus en France (`countries_tags_en=france`) | **1 255 052** |

**Verdict : exploitable, et largement.** 1,26 million de références pour la France, sur un pays qui compte quelques dizaines de milliers de références en circulation courante dans la grande distribution : la couverture des produits emballés de marque nationale sera très bonne. OFF est un projet d'origine française, la France est son marché historique et le mieux documenté.

**Nuance importante — présence ≠ complétude.** Une fiche peut exister avec un simple nom et aucune donnée nutritionnelle, aucune catégorie, aucune photo. **Je n'ai pas pu mesurer les taux de complétude** (Nutri-Score renseigné, photo de face sélectionnée) : les requêtes ont été bloquées par le rate limit de recherche **[NV]**. À mesurer proprement sur un dump JSONL local plutôt qu'en tapant l'API.

**Angles morts attendus [NV, non quantifiés] :** marques de distributeur régionales, produits de petits producteurs, produits en circuit court, épicerie fine, produits importés de niche.

### 3.4 Alternatives et compléments

| Source | Modèle | Verdict pour Pantry |
|---|---|---|
| **CodeOnline Food (GS1 France)** | Base alimentée **par les marques elles-mêmes**, donc données fiables et à jour, spécifiquement France. API « CodeOnline Search ». **Accès réservé aux adhérents GS1 France** ; la grille tarifaire cite un forfait PREMIUM à **20 000 € HT/an** **[S]** (<https://developers.gs1.fr/tarifs>) | **Hors de portée.** C'est pourtant *le* plan B qualitativement supérieur si le projet devenait commercial. |
| **Edamam Food Database** | Freemium, jusqu'à ~999 $/mois ; ~700 000 codes UPC/EAN **[S, non vérifié à la source]** | Base à dominante américaine, couverture FR douteuse. |
| **Nutritionix** | Entreprise, à partir de ~1 850 $/mois **[S, NV]** | Même remarque, et disqualifié par le prix. |
| **Barcode Lookup / Go-UPC / EAN-DB** | Lookup générique (pas nutritionnel). Barcode Lookup à partir de ~9 $/mois, 1 000 lookups/jour ; EAN-DB à ~0,005 €/code **[S, NV]** | Utile seulement comme **filet pour le nom du produit** quand OFF renvoie 404. Coût marginal réel pour un usage domestique. À garder en réserve, pas en v1. |
| **Open Prices** (projet OFF) | Open data, prix relevés par la communauté | Hors périmètre v1, mais intéressant plus tard pour un budget courses. |
| **L'utilisateur lui-même** | Gratuit | **C'est le vrai plan B.** Voir §4.1. |

**Recommandation :** OFF seul en v1, avec saisie manuelle en repli. Aucune API payante. Si un besoin de couverture apparaît, le mesurer d'abord (compter les 404 réels sur *son propre* placard) avant d'acheter quoi que ce soit.

### 3.5 Stratégie de cache côté backend

**Le point d'architecture le plus important de la section.**

La doc OFF précise : *« If your requests come from your users directly (ex: mobile app), the rate limits apply per user »* **[V]**. Corollaire, souvent manqué : **en centralisant les appels dans le backend FastAPI, toutes les requêtes sortent d'une IP unique — la limite de 15 req/min devient un plafond global, partagé par l'ensemble des utilisateurs de Pantry.**

Ce n'est pas une raison pour appeler OFF depuis le navigateur (on perdrait le cache, la maîtrise du User-Agent et la résilience hors ligne). C'est une raison pour que **le backend ne soit presque jamais amené à appeler OFF**.

La doc OFF le dit d'ailleurs elle-même : *« If you expect your app to generate a lot of API traffic, we **strongly encourage you to host a local instance** […] and use the daily exports to update your local database »* **[V]**.

**Architecture proposée :**

1. **Table `product_cache` en PostgreSQL** (conforme au défaut projet), clé primaire = EAN normalisé, **globale et non scopée par foyer** (voir §3.6). Colonnes : les champs utiles dénormalisés, plus le JSON brut, plus `fetched_at`, `source` (`off` / `manual` / `import`), `off_last_modified_t`.
2. **Cache positif quasi permanent.** Une fiche produit ne change presque jamais. TTL long (30 jours), servi en **stale-while-revalidate** : on rend immédiatement la version en cache, on rafraîchit en tâche de fond. Le scan ne doit *jamais* attendre le réseau.
3. **Cache négatif court.** Un 404 doit être mémorisé — sinon chaque re-scan d'un produit absent retape OFF — mais avec un TTL court (24 h) car un produit peut être ajouté à OFF entre-temps, y compris par l'utilisateur lui-même.
4. **Un seul point de sortie, avec limiteur.** Toutes les requêtes OFF passent par un client unique portant :
   - le User-Agent conforme (`Pantry/x.y (contact@…)`) ;
   - un limiteur à **10 req/min** (marge sous les 15) ;
   - un backoff exponentiel sur 429/503, et une tolérance aux réponses **HTML** (voir §3.2) ;
   - un timeout court (2–3 s) : OFF est une infrastructure associative, pas un CDN.
5. **Pré-remplissage par dump.** Le levier décisif. OFF publie un dump MongoDB nocturne, un export **JSONL gzip**, un **Parquet** sur Hugging Face, un CSV (~0,9 Go compressé / ~9 Go décompressé) et des **exports delta sur fenêtre glissante de 14 jours** **[V]** (<https://world.openfoodfacts.org/data>). Importer une fois le sous-ensemble « vendus en France » avec les 10 champs utiles, puis appliquer les deltas quotidiennement, ramène le taux de hit réseau proche de zéro. Volume estimé : ~1,26 M lignes × quelques centaines d'octets ≈ **quelques centaines de Mo en Postgres** — parfaitement raisonnable. Chantier de v2, pas de v1, mais **concevoir la table dès la v1 pour pouvoir être alimentée par les deux voies**.
6. **Images.** Ne pas hotlinker `images.openfoodfacts.org` depuis le client à chaque affichage de liste : c'est de la charge gratuite sur l'infra OFF. Proxy + cache disque côté backend, ou téléchargement de la vignette à la première résolution. Conserver l'attribution CC BY-SA.
7. **Dev sur le staging.** `world.openfoodfacts.net` (Basic Auth `off`/`off`) pour tous les tests, comme demandé.

### 3.6 Articulation avec le multi-tenant (ADR 0006)

L'[ADR 0006](adr/0006-multi-tenant-from-day-one.md) acte un modèle multi-tenant dès la première migration, avec une phase 2 d'ouverture publique multi-utilisateurs. Deux conséquences directes sur ce module :

**a) Le cache OFF n'est pas une donnée de foyer.** L'ADR impose `household_id` sur « toute table métier » et cite `UNIQUE (household_id, barcode)`. Cette contrainte est correcte pour l'**article en stock**, mais **la fiche produit issue d'OFF n'est pas une donnée de foyer** : c'est un cache de référentiel externe, identique pour tout le monde. Le cacher par foyer multiplierait les appels à OFF par le nombre de foyers — exactement ce que le plafond de 15 req/min interdit.

Le découpage à retenir est donc en **deux tables distinctes** :
- `product_cache` — **globale, sans `household_id`**, clé `barcode`, alimentée par OFF ou par le dump. Aucune donnée personnelle, donc aucun enjeu d'étanchéité.
- `item` / `stock_entry` — **par foyer**, avec `household_id`, portant les surcharges locales (§4.5) et le stock.

Cette séparation n'est pas une entorse à l'ADR : elle en respecte l'esprit (toute donnée *métier* est scopée) tout en évitant de scoper un cache partagé. **À expliciter dans l'ADR ou dans le modèle de données**, sinon quelqu'un ajoutera un `household_id` à `product_cache` par application mécanique de la règle.

**b) Le plafond de 15 req/min devient un mur en phase 2.** En mono-foyer, un cache correct suffit. En service public multi-utilisateurs, 15 requêtes produit par minute partagées entre *tous* les foyers, depuis l'IP unique du backend, ne tient pas — et le dépassement expose à un bannissement d'IP qui couperait le service pour tout le monde d'un coup. **L'import du dump JSONL n'est donc pas une optimisation de confort mais un prérequis de la phase 2**, à traiter comme tel dans la feuille de route.

**c) Le share-alike ODbL se réveille en phase 2.** Tant que Pantry sert un foyer, la question de la redistribution est théorique. Un service public qui combine OFF avec d'autres sources de données produit entre dans le périmètre du share-alike (§3.2). À trancher **avant** l'ouverture, pas après.

---

## 4. Ce qui va mal se passer

Section volontairement pessimiste. Chaque mode d'échec est suivi de son repli UX. Ce sont ces replis qui font la différence entre une app utilisable et une démo.

### 4.1 Le produit n'est pas dans Open Food Facts (HTTP 404)

**Fréquence attendue :** faible sur les marques nationales, **élevée** sur les MDD régionales, les producteurs locaux, l'épicerie fine, les produits importés. **[NV]** — non quantifié.

**Repli :** le 404 ne doit **jamais** être une impasse. L'écran de scan enchaîne directement sur un formulaire pré-rempli avec l'EAN, ne demandant que **trois champs** : nom, marque (facultative), quantité. Le produit entre au stock immédiatement. Optionnellement, proposer une contribution à OFF (photo de face + nom) : les écritures ne sont pas rate-limitées **[V]** et la doc OFF encourage explicitement ce flux pour les « inventory apps ». C'est un cercle vertueux : l'utilisateur enrichit la base dont il dépend.

**Anti-pattern à éviter :** un message « produit inconnu » avec un bouton « OK ». C'est ce qui fait abandonner une app d'inventaire à la troisième utilisation.

### 4.2 Le code-barres est illisible

Emballage froissé (sachets souples, surgelés), reflet sur film plastique, code partiellement recouvert par une étiquette de prix, bouteille cylindrique de faible diamètre, lumière basse (placard, cellier), tremblement, code trop petit sur un conditionnement individuel.

**Replis, dans l'ordre :**
1. **Guider avant de corriger.** Cadre de visée à l'écran, retour haptique/sonore au décodage, message contextuel après ~3 s d'échec (« rapprochez-vous », « évitez le reflet »).
2. **Torche** — mais bouton présent uniquement si `"torch" in track.getCapabilities()`. **Absent sur iPhone** (§2.1). C'est une asymétrie iOS/Android qu'il faut accepter.
3. **Zoom numérique** via `applyConstraints({ zoom })` si la capacité existe — aide sur les codes petits.
4. **Saisie manuelle des 13 chiffres**, toujours accessible d'un tap depuis l'écran de scan. Ce n'est pas un aveu d'échec, c'est le filet indispensable. **Valider la clé de contrôle EAN-13 en local** avant tout appel réseau : ça détecte immédiatement une faute de frappe et évite un 404 trompeur.
5. **Ne pas s'acharner en boucle.** Après ~10 s sans décodage, proposer explicitement la saisie manuelle plutôt que de laisser tourner la caméra.

### 4.3 Le produit n'a pas de code-barres du tout

Fruits et légumes en vrac, boucherie, poissonnerie, fromage à la coupe, boulangerie, vrac sec, jardin et conserves maison.

**Ce n'est pas un cas marginal.** Dans un placard et un frigo réels, cette catégorie représente une part significative du contenu. Une app de stock alimentaire qui ne sait ajouter qu'au scan est structurellement incomplète.

**Replis :**
1. **L'ajout manuel est un chemin de premier rang**, pas une option cachée. Bouton « + » toujours visible à côté du scan.
2. **Catalogue local de produits génériques** : « pommes », « carottes », « bœuf haché », « pain ». Une trentaine d'entrées couvrent l'essentiel du frais domestique. Réutilisables, avec unité (pièce / g / kg) et durée de conservation par défaut.
3. **Codes PLU** (Price Look-Up, standard IFPS) : les étiquettes 4–5 chiffres sur les fruits et légumes. 4 chiffres = culture conventionnelle, 5 chiffres commençant par 9 = bio **[S]** (<https://www.ifpsglobal.com/>). Reconnaissables optiquement mais **il ne s'agit pas d'un code-barres** — il faudrait de l'OCR. **À ne pas faire en v1** ; une liste de sélection est plus rapide pour l'utilisateur qu'une reconnaissance approximative.
4. **Produits récurrents** : proposer en tête de liste ce que l'utilisateur ajoute souvent. Deux taps pour « 6 pommes ».

### 4.4 Poids variables et codes internes magasin

Les codes-barres à préfixe **02** et **20–29** sont des *Restricted Circulation Numbers* : GS1 les réserve à l'usage interne des distributeurs **[S]** (<https://www.gs1.org/docs/barcodes/SummaryOfGS1MOPrefixes20-29.pdf>, <https://www.gs1uk.org/knowledge-hub/barcodes/how-to-barcode-variable-measure-items>). On les trouve sur tout ce qui est pesé en magasin : barquettes de boucherie, fromage à la coupe, fruits pesés en caisse. Leur structure typique encode une référence article interne **et le prix ou le poids**, selon une convention **propre à chaque enseigne**.

**Deux conséquences directes :**
1. Ces codes **ne sont pas dans OFF et n'y seront jamais.** Les interroger, c'est garantir un 404 et consommer inutilement le quota de 15 req/min.
2. **Le même produit a un code différent d'un ticket à l'autre** (le prix change avec le poids). Les mettre en cache produirait des milliers d'entrées inutiles.

**Repli :** détecter le préfixe **côté client** (`ean.startsWith("02") || /^2[0-9]/.test(ean)`), **ne pas appeler le backend**, et basculer directement sur le formulaire manuel avec un message honnête : « code interne magasin — décrivez le produit ». Décoder le poids ou le prix embarqué est possible mais dépend de l'enseigne : **à ne pas tenter en v1**.

### 4.5 La fiche OFF existe mais elle est fausse ou incomplète

Données contributives : nom en majuscules, marque mal orthographiée, `quantity` en texte libre incohérent, catégories absurdes, ancienne version d'une recette, Nutri-Score obsolète. OFF le dit lui-même : *« no assurances that the data is accurate, complete, or reliable »* **[V]**.

**Repli :** toute fiche importée doit être **modifiable localement**, et la modification locale doit **primer** sur un rafraîchissement OFF ultérieur (colonne `source`/`overridden_at` dans le schéma — à prévoir dès la première migration, l'ajouter après est douloureux).

**Piège de parsing :** `quantity` est du texte (`"400.0 g"`, `"1L"`, `"6x125g"`, `"environ 250 g"`). Ne jamais supposer un format. Parser au mieux, conserver la chaîne d'origine, et **afficher le texte brut en cas d'échec** plutôt qu'un `null` ou un `0`.

### 4.6 iOS redemande l'autorisation caméra

Voir §2.2. **[V]** — bugs WebKit toujours actifs.

**Repli :** ne pas déclencher `getUserMedia()` au montage de l'écran. Afficher d'abord un état explicite avec un bouton « Activer la caméra » : une demande d'autorisation déclenchée par un geste utilisateur est mieux comprise, et si elle est redemandée, elle ne ressemble pas à un bug. Gérer `NotAllowedError` avec un message qui explique *où* réautoriser (Réglages > Safari), et un bouton « Réessayer ». Et garder le retrait d'`apple-mobile-web-app-capable` comme interrupteur de secours documenté.

### 4.7 Scans parasites et doublons

Une caméra qui tourne décode le même code 30 fois par seconde. Et un utilisateur qui range ses courses scanne parfois deux fois le même article — sans savoir si c'est un doublon ou deux exemplaires.

**Repli :** anti-rebond sur l'EAN (ignorer le même code pendant ~2 s), et une confirmation explicite qui affiche le produit reconnu avec un compteur de quantité incrémentable. Le mode « courses » (scan en rafale de 20 articles) et le mode « ajout unitaire » ont des attentes UX différentes — à distinguer.

### 4.8 Décodage erroné

Rare avec la validation de checksum de ZXing, mais possible sur code partiellement masqué, et plus probable si l'on active tous les formats.

**Replis :** restreindre `formats` aux formats commerce de détail (§1.3) ; **valider la clé de contrôle EAN-13 côté client** avant lookup ; exiger **deux lectures identiques consécutives** avant de valider (peu coûteux, élimine l'essentiel des faux positifs).

### 4.9 Open Food Facts est indisponible

Infrastructure associative. Pannes, maintenances, rate limit atteint, **et réponses HTML au lieu de JSON** (constaté §3.2).

**Repli :** le cache Postgres absorbe la majorité des cas. Sinon, l'ajout se fait avec l'EAN seul, en file d'attente d'enrichissement à traiter plus tard en tâche de fond. **L'indisponibilité d'OFF ne doit jamais empêcher d'ajouter un article au stock.**

---

## 5. Décisions recommandées

1. **Un seul décodeur : `barcode-detector` (ponyfill Sec-ant, MIT, v3.2.1) sur `zxing-wasm`.** Pas de branche « API native si disponible » : elle n'existe ni sur iOS, ni sur Firefox, ni sur Chrome/Linux — donc jamais testable en dev local — et le gain de perf ne justifie pas un second chemin de code. Formats restreints à `ean_13, ean_8, upc_a, upc_e, databar*`. WASM chargé en lazy à l'ouverture de l'écran de scan (~450 Ko gzip, mesuré) et **précaché explicitement** par le service worker.

2. **Écarter `html5-qrcode`** (dernière release npm avril 2023, mode maintenance déclaré, 441 issues ouvertes, embarque une version figée de `@zxing/library`). Écarter `@zxing/library` en direct (JS pur donc plus lent, 20 mois sans release entre 2024 et 2026, 170 issues).

3. **Valider la caméra iOS sur appareil réel avant tout engagement.** C'est le seul point qui peut remettre en cause la viabilité mobile. Le support existe depuis iOS 13.4 (#185448 RESOLVED FIXED), mais les bugs de non-persistance des permissions (#215884, rapports jusqu'en janvier 2026) et de flux vidéo noir en mode standalone (#252465, régressions jusqu'en juin 2025) sont documentés et actifs. **Prototype jetable, une demi-journée, avant d'écrire le module.** Garder le retrait d'`apple-mobile-web-app-capable` comme interrupteur de secours documenté.

4. **Offline-first, sans Background Sync.** Non supporté par Safari. File d'attente en IndexedDB, vidée sur `online` / `visibilitychange` / démarrage. Le scan et le décodage doivent fonctionner en avion ; seule la résolution EAN → fiche exige le réseau, et elle est asynchrone par conception.

5. **Open Food Facts en v3 (v3.6) uniquement.** v2 est officiellement dépréciée. Toujours `fields=` pour ne demander que le nécessaire. User-Agent `Pantry/x.y (contact@…)` obligatoire. Développement contre le staging `world.openfoodfacts.net` (Basic Auth `off`/`off`). Remplir le formulaire de déclaration d'usage de l'API.

6. **Le rate limit est un plafond global, pas par utilisateur.** 15 req/min pour une IP unique : en centralisant dans FastAPI, c'est la limite de **toute** l'application. Donc : cache Postgres avec TTL long en stale-while-revalidate, cache négatif court (24 h) sur les 404, un seul client sortant avec limiteur à 10 req/min et backoff, tolérance aux réponses HTML. **À terme, pré-remplir la base par le dump JSONL « France » + deltas quotidiens** — OFF le recommande explicitement. Concevoir la table dès la v1 pour accepter les deux voies d'alimentation.

7. **Le cache OFF est une table globale, sans `household_id`** — contrairement à ce que la règle de l'ADR 0006 laisserait supposer par application mécanique. Une fiche produit est un référentiel externe partagé, pas une donnée de foyer ; la scoper multiplierait les appels à OFF par le nombre de foyers. Séparer `product_cache` (globale) de `item` / `stock_entry` (par foyer, portant les surcharges locales), et l'expliciter dans le modèle de données. **Deux points sont à traiter avant l'ouverture publique de la phase 2 : l'import du dump devient un prérequis** (15 req/min partagés entre tous les foyers, avec risque de bannissement d'IP coupant le service pour tout le monde) **et le share-alike ODbL cesse d'être théorique** si l'on combine OFF avec d'autres bases.

8. **Couverture OFF suffisante : 1 255 052 produits vendus en France sur 4 663 574 au total** (mesuré le 3 août 2026). Pas de plan B payant en v1. CodeOnline Food (GS1 France) serait qualitativement supérieur mais son accès passe par une adhésion GS1 avec des forfaits à cinq chiffres — hors de portée. Mesurer le taux réel de 404 sur son propre placard avant d'envisager quoi que ce soit d'autre.

9. **La saisie manuelle est une fonctionnalité de premier rang, pas un repli.** Produits absents d'OFF, codes illisibles, frais sans code-barres, codes internes magasin à préfixe 02/20–29 : ces cas cumulés représentent une part substantielle d'un stock domestique réel. **Une app qui ne sait ajouter qu'au scan est inutilisable.** Corollaire de schéma : les fiches doivent être éditables localement, et l'édition locale doit primer sur tout rafraîchissement OFF ultérieur — prévoir la colonne dès la première migration.

---

## Sources

**Support navigateur**
- MDN browser-compat-data, `api/BarcodeDetector.json` — <https://github.com/mdn/browser-compat-data/blob/main/api/BarcodeDetector.json>
- MDN, `BarcodeDetector` — <https://developer.mozilla.org/en-US/docs/Web/API/BarcodeDetector>
- caniuse, BarcodeDetector API — <https://caniuse.com/mdn-api_barcodedetector>
- Chrome for Developers, Shape Detection API — <https://developer.chrome.com/docs/capabilities/shape-detection>
- caniuse, Background Sync API — <https://caniuse.com/background-sync>
- WebKit Features in Safari 26.0 — <https://webkit.org/blog/17333/webkit-features-in-safari-26-0/>
- WebKit Features for Safari 26.6 — <https://webkit.org/blog/18178/webkit-features-for-safari-26-6/>

**Bugs WebKit**
- #185448 — getUserMedia en mode standalone (RESOLVED FIXED, iOS 13.4) — <https://bugs.webkit.org/show_bug.cgi?id=185448>
- #215884 — persistance des permissions caméra en PWA — <https://bugs.webkit.org/show_bug.cgi?id=215884>
- #252465 — flux vidéo noir en PWA iOS — <https://bugs.webkit.org/show_bug.cgi?id=252465>
- #281848 — Shape Detection API non fonctionnelle sur iOS (ouvert) — <https://bugs.webkit.org/show_bug.cgi?id=281848>

**Bibliothèques** (métadonnées relevées le 2026-08-03 sur `registry.npmjs.org` et `api.github.com`)
- zxing-wasm — <https://github.com/Sec-ant/zxing-wasm>
- barcode-detector — <https://github.com/Sec-ant/barcode-detector>
- @zxing/library — <https://github.com/zxing-js/library>
- html5-qrcode — <https://github.com/mebjas/html5-qrcode>
- @ericblade/quagga2 — <https://github.com/ericblade/quagga2>

**Caméra**
- STRICH KB, Camera Access Issues in iOS PWA — <https://kb.strich.io/article/29-camera-access-issues-in-ios-pwa>
- Dynamsoft, camera focus control on web — <https://www.dynamsoft.com/codepool/camera-focus-control-on-web.html>
- Scandit, make a barcode scanner app performant — <https://www.scandit.com/blog/make-barcode-scanner-app-performant/>

**Open Food Facts**
- Documentation API (source) — <https://github.com/openfoodfacts/openfoodfacts-server/blob/main/docs/api/index.md>
- Version publiée — <https://openfoodfacts.github.io/openfoodfacts-server/api/>
- Data, API and SDKs (exports, licences) — <https://world.openfoodfacts.org/data>
- Conditions d'utilisation — <https://world.openfoodfacts.org/terms-of-use>
- ODbL 1.0 — <https://opendatacommons.org/licenses/odbl/1.0/> · DbCL 1.0 — <https://opendatacommons.org/licenses/dbcl/1.0/> · CC BY-SA 3.0 — <https://creativecommons.org/licenses/by-sa/3.0/>

**Codes-barres et alternatives**
- GS1, préfixes 20–29 — <https://www.gs1.org/docs/barcodes/SummaryOfGS1MOPrefixes20-29.pdf>
- GS1 UK, variable measure items — <https://www.gs1uk.org/knowledge-hub/barcodes/how-to-barcode-variable-measure-items>
- IFPS (codes PLU) — <https://www.ifpsglobal.com/>
- GS1 France, CodeOnline for Developers — tarifs — <https://developers.gs1.fr/tarifs>
