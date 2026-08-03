# Note technique — Ingestion automatique du stock et export de la liste de courses

**Projet** : Pantry (PWA de gestion de stock alimentaire domestique — backend FastAPI / PostgreSQL)
**Statut** : note de faisabilité, pour décision
**Date de rédaction** : 3 août 2026
**Toutes les pages citées ont été consultées le 3 août 2026.** Les tarifs, quotas et politiques évoluent vite : les chiffres ci-dessous ont une durée de validité de quelques mois, pas d'années.

---

## 0. Périmètre et méthode

### 0.1 Ce qui est déjà tranché et n'est pas rediscuté ici

**L'intégration des comptes drive des enseignes (Courses U, Intermarché Drive, Chronodrive, Auchan Drive…) est écartée.** Aucune de ces enseignes n'expose d'API publique ; l'accès passerait par du reverse-engineering d'endpoints privés, fragile par construction et contraire aux CGU. Le point est tranché par **[ADR-0002](adr/0002-no-retailer-drive-integration.md)** (accepté le 2026-08-03) et n'est pas rediscuté ici. La présente note part donc du principe que les données d'achat doivent venir **de l'utilisateur** (mail de récap qu'il reçoit déjà, ou photo de son ticket), jamais d'un scraping d'enseigne.

Cette note **instruit les voies 3 et 4 de l'ADR-0002** (photo de ticket, capture d'e-mail transféré) et documente l'export de la liste de courses, qui n'est couvert par aucun ADR à ce jour.

Un signal qui va dans le même sens pour l'avenir : la loi anti-gaspillage a supprimé l'impression automatique du ticket de caisse au 1er août 2023, ce qui pousse structurellement les enseignes vers le ticket dématérialisé — donc vers l'email et le QR code, c'est-à-dire vers le chemin n°1 de cette note.
Source : <https://www.presse-citron.net/le-ticket-de-caisse-disparait-quel-est-son-remplacant/>

### 0.2 Rattachement aux décisions déjà prises

Cette note ne part pas d'une page blanche. Elle doit se lire avec :

| Document | Ce qu'il impose à cette note |
|---|---|
| [ADR-0002](adr/0002-no-retailer-drive-integration.md) | Pas de drive enseigne. L'adresse par foyer est spécifiée `<household_token>@inbox.<domain>` — c'est ce format que §1 met en œuvre. |
| [ADR-0005](adr/0005-llm-provider-abstraction.md) | **Le fournisseur de modèle est une donnée, pas une constante.** Cinq adaptateurs (Anthropic, OpenAI, Gemini, Mistral, Ollama), dégradation par capacités détectées. Le §3 ne peut donc pas « choisir un modèle » : il peut recommander un **défaut** et chiffrer des repères. |
| [ADR-0007](adr/0007-byok-and-local-inference.md) | **BYOK : c'est le foyer qui paie.** Aucun budget commun. Deux configurations gardent les données sous juridiction UE : `byok` Mistral (hébergé UE) et `ollama` (rien ne sort). L'inférence locale n'est pas un repli dégradé. |
| [ADR-0006](adr/0006-multi-tenant-from-day-one.md) | `household_id` sur toute table métier — donc sur l'adresse d'inbox, sur les tickets et sur la table d'alias de §3.6. |
| [ADR-0008](adr/0008-open-food-facts-integration.md) + [`technical-notes-scanning.md`](technical-notes-scanning.md) | La stratégie Open Food Facts est **déjà tranchée** (cache d'abord, catalogue partagé `household_id IS NULL`, API v3, dump local en prérequis phase 2). Le §3.6 s'y raccorde au lieu de la rejouer. |

### 0.3 Les quatre questions traitées

1. Réception d'emails entrants (adresse dédiée par foyer + webhook) — **voie recommandée**
2. Lecture directe de la boîte mail (Gmail API / IMAP) — voie alternative, documentée pour comparaison
3. Parsing des tickets de caisse par modèle multimodal
4. Export de la liste de courses vers les apps que les gens utilisent déjà

### 0.4 Limites de cette recherche

La vérification a été menée par consultation directe des pages officielles. Plusieurs sites en rendu 100 % JavaScript (docs Stalwart, tarifs Brevo, `dev.mailjet.com`, portails OVH/Infomaniak) et quelques pages en 403 n'ont pas pu être lus. **Chaque point non vérifié est signalé explicitement en ligne**, et une liste récapitulative figure en §6. Les affirmations non sourcées sont des raisonnements d'ingénierie, signalés comme tels.

---

## 1. Réception d'emails entrants — la voie recommandée

### 1.1 Le principe retenu

On attribue à chaque foyer une adresse dédiée — format fixé par l'ADR-0002 : `<household_token>@inbox.<domain>`, par exemple `u7f3a@inbox.exemple.fr`. L'utilisateur crée dans son client mail une **règle de transfert** ciblant les expéditeurs d'enseignes, et le mail transféré arrive sur un webhook HTTP qui le parse.

C'est simple à décrire. Trois choses le compliquent, et elles décident du choix de fournisseur.

### 1.2 Le point structurant : un transfert casse SPF, et certains fournisseurs rejettent pour ça

C'est **le** critère de sélection, et il est presque toujours ignoré des comparatifs.

- **SPF échoue systématiquement sur un transfert.** SPF est une liste de serveurs autorisés à émettre pour un domaine ; le serveur de Google qui reforwarde le mail de Carrefour n'y figure évidemment pas, et il n'est pas envisageable de maintenir une liste de forwarders.
  Source : <https://dmarcian.com/forwarding-and-dmarc/>
- **DKIM survit** si le forwarder ne modifie ni le corps ni les headers signés. Google documente que toucher aux frontières MIME, au sujet ou aux headers `To`/`Cc`/`Date`/`Message-ID` casse la signature, et que « *Messages that don't pass DKIM are more likely to be sent to spam* ».
  Source : <https://support.google.com/a/answer/175365?hl=en>
- **Conséquence** : « *for forwarded email, your DMARC compliance is equal to the "survival" of your DKIM signatures* » (dmarcian, même URL). Un expéditeur qui signe bien passe ; un qui signe mal est en échec DMARC total.
- **SRS ne répare pas ce qu'on croit.** Le Sender Rewriting Scheme réécrit l'enveloppe pour faire passer SPF, mais l'alignement DMARC reste cassé puisque le `From:` visible ne change pas. Microsoft l'écrit noir sur blanc : « *SRS rewriting doesn't resolve the issue of forwarded messages not passing DMARC checks* ».
  Source : <https://learn.microsoft.com/en-us/exchange/reference/sender-rewriting-scheme>
  Au demeurant SRS ne nous concerne pas directement : c'est Google qui forwarde, nous sommes du côté receveur.
- **ARC est le vrai sauveur.** Google appose une chaîne ARC quand il forwarde, permettant au receveur final de faire confiance au verdict d'authentification d'origine. Encore faut-il que le fournisseur inbound l'honore.

**Application directe — c'est ce qui disqualifie Cloudflare pour ce cas d'usage** :

> Cloudflare exige que l'inbound passe une authentification (« *The email must either pass SPF or be correctly signed with DKIM* ») et **applique la politique DMARC de l'expéditeur** : « *messages failing sender DMARC policies are rejected* ».
> Source : <https://developers.cloudflare.com/email-routing/postmaster/>

Il existe des plaintes publiques sur exactement ce scénario :
<https://community.cloudflare.com/t/emails-forwarded-from-gmail-are-being-dropped-due-to-dmarc-checks-failing/849579>
<https://community.cloudflare.com/t/forward-to-gmail-dmarc-failure/565909>

À l'inverse, Postmark, Mailgun, SendGrid et Amazon SES **ne rejettent pas d'office sur DMARC** : ils calculent un score et nous laissent décider. Et un serveur auto-hébergé nous donne le contrôle total — on choisit de ne rien rejeter. C'est un argument de fond en faveur de l'auto-hébergement pour ce cas précis.

### 1.3 Frictions d'onboarding à budgéter (souvent sous-estimées)

| Contrainte | Détail | Source |
|---|---|---|
| **Gmail exige une vérification de l'adresse de destination** | « *After you add a forwarding email address, we send a verification link to the address. After you verify, you can forward messages* » | <https://support.google.com/mail/answer/9414102?hl=en> |
| **Gmail ne transfère pas le spam** | « *We forward all new messages to the account, **except for spam*** » | idem |
| **Transfert sélectif possible** | Un filtre Gmail « Forward it » permet de ne transférer que les mails de l'enseigne — c'est ce qu'il faut recommander à l'utilisateur (minimisation RGPD) | idem |
| **Microsoft 365 bloque l'auto-forwarding externe par défaut** | Politique anti-spam sortante ; action admin requise | <https://woshub.com/enable-external-forwarding-microsoft-365-exchange/> — ⚠️ blog, **non confirmé sur learn.microsoft.com** |
| **Outlook.com impose la 2FA** pour activer le transfert | | <https://support.microsoft.com/en-us/office/turn-on-or-off-automatic-forwarding-in-outlook-com-6246987c-6c8f-4144-b255-14fc07007dad> |

**Exigence fonctionnelle qui en découle** : le mail de confirmation Gmail arrive sur l'adresse dédiée **avant** que le transfert ne soit actif. Notre webhook doit savoir le reconnaître et remonter le code/lien dans l'UI, sinon l'onboarding est bloqué. Ce n'est pas un détail, c'est une story à part entière.

### 1.4 Comparatif des fournisseurs managés

| Fournisseur | Entrée de gamme | Format webhook | Pièces jointes | Auth webhook | Taille max | Résidence UE | Rejet DMARC ? |
|---|---|---|---|---|---|---|---|
| **CloudMailin** | **gratuit, 10 000/mois** (512 KB max) ; utile à 45 $/mois | JSON normalisé / multipart / **raw MIME** | base64 **ou URL** vers store ; upload S3/Azure/GCS | 🟠 basic auth (signature **dépréciée**) | 512 KB → 50 MB selon plan | ✅ **forçable par DNS** | non |
| **ImprovMX** | 9 $/mois (Premium ; webhooks **exclus du gratuit**) | JSON complet + `?raw_mime=true` | base64 inline + `inlines[]` avec `cid` | 🔴 **aucune** (IP `15.237.103.194`) | non documentée | ✅ **datacenters FR (OVH)** | non |
| **Amazon SES** | 0,10 $/1 000 reçus + 0,09 $/1 000 chunks 256 KB | ❌ pas de webhook natif — SNS / Lambda / S3 | via S3 | 🟢 signature SNS / IAM | **150 KB en SNS**, 40 MB en S3 | ✅ **Paris (eu-west-3)**, Francfort… | non (verdicts exposés, décision à nous) |
| **Mailgun** | **gratuit, 1 route, 100/jour** ; Foundation 35 $ | multipart parsé, ou MIME brut si l'URL finit par `mime` | multipart + `content-id-map` ; `store()` 3 j | 🟡 HMAC (`timestamp`/`token`/`signature`) | non documentée | ⚠️ envoi UE annoncé, **MX EU non vérifiables** | non |
| **Postmark** | **16,50 $/mois** (Pro — inbound absent de Free et Basic) | JSON riche (`TextBody`, `HtmlBody`, `StrippedTextReply`, `MailboxHash`) | **base64 inline** | 🟠 IP allowlist (4 IP US) | ⚠️ **non documentée** | ❌ **aucune mention** | non |
| **ForwardEmail** | **gratuit** (webhooks inclus, config par TXT DNS) | JSON `mailparser` + `raw`, avec verdicts `spf`/`dkim`/**`arc`**/`dmarc` | incluses (`?attachments=false`) | 🟢 **HMAC `X-Webhook-Signature`** (payant) + rDNS | **50 MB** | ❌ **Denver, Colorado** | non |
| **Brevo** | prix 2026 **non extractibles** | JSON très riche + `ExtractedMarkdownMessage` (signature retirée par ML), `Spam.Score` rspamd | métadonnées + `DownloadToken` | 🟡 IP / basic / **bearer** / headers | non documentée | 🇫🇷 réputé, **non confirmé ce jour** | non |
| **Resend** | gratuit 3 000/mois (in+out confondus) | ❌ **métadonnées seules** — 2 à 3 appels API pour le corps et les PJ | `download_url` valide 1 h | 🟢 **HMAC Svix** (anti-replay) | non documentée | ❌ « *All account data … is stored in the United States* » | non |
| **Mailtrap** | gratuit 4 000/mois ; inbound prod dès Basic 15 $ | métadonnées + API | via API | 🟢 **HMAC-SHA256** | non documentée | non mentionnée | non |
| **Cloudflare Email Routing** | **gratuit** | ❌ **MIME brut** à parser soi-même (`postal-mime` recommandé) | à extraire soi-même | 🟢 notre propre secret (c'est notre Worker qui `fetch`) | **25 MiB** | — | 🔴 **OUI — rédhibitoire** |
| **SendGrid Inbound Parse** | ⚠️ **tarifs non vérifiables** | `multipart/form-data`, option MIME brut | non URL-encodées (piège documenté) | 🔴 **aucune** documentée | **30 MB** (2,5 MB pour l'antispam) | ⚠️ non vérifiable | non |
| **Mailjet Parse API** | Free 6 000/mois — ⚠️ doc dit « Crystal and above », **plan inexistant dans la grille** | JSON + `Parts[]` + `AttachmentN` base64 | base64 | 🟠 basic auth | non documentée | ⚠️ **non vérifiable** (`/legal/dpa/` redirige vers sinch.com) | non |
| **Scaleway TEM** | — | ❌ **pas d'inbound du tout** : « *you can only **send*** » | — | — | — | ✅ fr-par | — |

Sources principales : <https://postmarkapp.com/pricing> · <https://postmarkapp.com/developer/webhooks/inbound-webhook> · <https://www.mailgun.com/pricing/> · <https://documentation.mailgun.com/docs/mailgun/user-manual/receive-forward-store/receive-http/> · <https://www.twilio.com/docs/sendgrid/for-developers/parsing-email/setting-up-the-inbound-parse-webhook> · <https://developers.cloudflare.com/email-routing/limits/> · <https://developers.cloudflare.com/email-routing/email-workers/> · <https://www.cloudmailin.com/plans-and-pricing> · <https://docs.cloudmailin.com/http_post_formats/> · <https://improvmx.com/guides/webhooks/> · <https://improvmx.com/pricing/> · <https://forwardemail.net/en/pricing> · <https://developers.brevo.com/docs/inbound-parse-webhooks> · <https://resend.com/docs/webhooks/emails/received.md> · <https://resend.com/docs/dashboard/domains/regions> · <https://docs.aws.amazon.com/ses/latest/dg/quotas.html> · <https://aws.amazon.com/ses/pricing/> · <https://www.scaleway.com/en/transactional-email-tem/>

#### Points saillants du tableau

- **Postmark** : l'inbound n'apparaît que sur Pro et Platform d'après la grille consultée. Le ticket d'entrée réel est donc **16,50 $/mois**, pas 0 $. ⚠️ La page ne dit pas si l'inbound consomme le quota des 10 000 emails, et **aucune taille max n'a pu être trouvée** (les articles de support pertinents renvoient 404). Retries : 10 tentatives en intervalles croissants, **et un 403 stoppe définitivement les retries** — ne jamais renvoyer 403 sur une erreur transitoire.
- **Mailgun** : le nombre de routes n'est **pas** le nombre d'adresses. Un seul `catch_all()` ou `match_recipient(".*@inbox.exemple.fr")` suffit, donc le plan gratuit (1 route) est techniquement viable sous 100 mails/jour. Le mode MIME brut se déclenche par l'**URL** (si elle finit par `mime` ou `raw-mime`), pas par la taille. `store()` retient 3 jours et notifie avec une URL de récupération — utile pour les grosses PJ qui feraient timeouter notre endpoint. ⚠️ La page décrivant l'algorithme HMAC renvoie 404 aujourd'hui : le mécanisme existe (`timestamp`, `token`, `signature` sont dans chaque POST) mais **ses modalités exactes n'ont pas pu être re-confirmées**.
- **Cloudflare** : gratuit et techniquement élégant, mais **trois défauts cumulés** — rejet sur DMARC (§1.2), plafond de **200 règles de routage par domaine** (mur pour « une adresse par foyer » ; obligation de passer par un catch-all + Worker), et MIME brut à parser soi-même. ⚠️ La question « faut-il que le domaine soit en full setup sur les nameservers Cloudflare ? » **n'a pas pu être tranchée** sur une page officielle ; c'est très probable puisque Cloudflare doit gérer les MX, mais ce n'est pas sourcé.
- **SendGrid** : les pages de tarification tournent en boucle de redirection (`sendgrid.com/pricing` → `twilio.com/en-us/sendgrid` → … ). **Impossible d'établir la grille 2026 ni de confirmer l'existence d'un plan gratuit permanent** sur une page officielle. La page produit ne mentionne qu'un « *free trial — no credit card required* », ce qui *suggère* un basculement vers un essai sans le prouver. Un blog concurrent daté du 27 février 2026 (<https://www.pingram.io/blog/best-inbound-email-notification-apis>) annonce « 100 emails/day for 30 days » puis 19,95 $/mois — **indication, pas fait**. S'ajoute l'absence totale de sécurité de webhook documentée. À écarter.
- **Resend** : le webhook ne contient **pas** le mail — « *Webhooks do not include the email body, headers, or attachments, only their metadata* ». Il faut un deuxième appel pour le corps, un troisième par pièce jointe. Et « *All account data, including email metadata, logs, and API records, is stored in the United States regardless of the sending region you select* » : les régions ne concernent **que l'envoi**. Le blog Pingram annonçant « EU region available (Ireland) » pour Resend est trompeur au regard de la doc officielle.
- **CloudMailin** : comportement HTTP→SMTP notable — il **ne retente pas** lui-même, il traduit notre code retour en réponse SMTP (4xx → SMTP 554 rejet définitif + notification à l'expéditeur ; 5xx → SMTP 450, l'émetteur retentera). Propre, mais un 500 accidentel de notre app renvoie la balle à Gmail pendant des jours. ⚠️ **`OpenAI (USA)` figure dans la liste des sous-traitants** pour « *Analysis and content detection* » (<https://www.cloudmailin.com/privacy>) — à clarifier contractuellement avant d'y faire transiter des tickets nominatifs.
- **Mailjet** : deux incohérences non levées. La doc officielle dit que la Parse API est réservée aux plans « Crystal and above », **or aucun plan « Crystal » n'existe dans la grille publique du 3 août 2026** (Free / Starter 9 $ / Essential 17 $ / Premium 27 $ / Custom). Et l'hébergement européen, pourtant sa réputation, **n'est vérifiable sur aucune page officielle** (`/legal/dpa/` redirige vers sinch.com, `/gdpr/` et `/legal/` en 403).
- **Brevo** : avertissement honnête de leur part, à intégrer dans notre design — « *100 % success rate on inbound parsing is impossible* ». Prévoir un chemin de secours quand `ExtractedMarkdownMessage` est vide ou tronqué.

**Écartés d'emblée** (hors sujet ou modèle économique absurde) : Mailparser.io (29,95 $/mois pour **250 emails**), Parseur, Zapier Email Parser (1 mail = 1 tâche facturée), Mailosaur (outil de QA), Nylas (connecte les boîtes existantes par OAuth, ne fournit pas d'adresse dédiée sur notre domaine), Zoho Mail (API de lecture seule, pas de push), Tuta (aucune API mail — le chiffrement bout-en-bout propriétaire rend l'intégration serveur structurellement impossible), *anymail finder* (outil de prospection B2B, à ne pas confondre avec la bibliothèque `django-anymail` qui, elle, normalise les webhooks inbound de plusieurs ESP derrière une API unique — pertinente si on veut garder la portabilité entre fournisseurs).

### 1.5 L'option auto-hébergée : raisonnable ici, et même préférable

**Oui, et pour de bonnes raisons.** Le contexte est favorable : on ne fait que **recevoir**, sur un VPS qu'on a déjà.

#### État des projets (API GitHub, interrogée le 3 août 2026)

| Projet | ★ | Licence | Dernier push | Hook HTTP natif |
|---|---|---|---|---|
| **Stalwart** | 13 996 | **AGPL-3.0-only OR SELv2** (dual) | 2026-08-03 (release v0.16.16 le 02/08) | ✅ **MTA Hooks** |
| **Postal** | 16 715 | **MIT** | 2026-08-03 | ✅ **natif** |
| Haraka | 5 613 | MIT | 2026-08-03 | ⚠️ à écrire soi-même |
| Maddy | 6 052 | GPL-3.0 | 2026-07-24 | ⚠️ aucun trouvé |
| `remi-san/haraka-http-queue` | **2** | Apache-2.0 | **2014-08-14** | ❌ mort depuis 12 ans |

#### Stalwart — MTA Hooks, « comme milter mais en HTTP »

C'est exactement ce qu'il nous faut. Le CHANGELOG note « *Pipes have been deprecated in favor of MTA hooks* ».

Structures vérifiées **dans le code source** (<https://raw.githubusercontent.com/stalwartlabs/stalwart/main/crates/smtp/src/inbound/hooks/mod.rs>) :

- **Stages** : `connect`, `ehlo`, `auth`, `mail`, `rcpt`, **`data`**.
- **Requête JSON** : `{context: {stage, client{ip,port,ptr,helo}, tls, server, queue{id}}, envelope: {from, to[]}, message: {headers[], serverHeaders[], contents, size}}` — **au stage `data`, `message.contents` contient le message complet**.
- **Réponse attendue** : `{action: "accept"|"discard"|"reject"|"quarantine", response: {...}, modifications: [...]}`.
- **Client HTTP** (`client.rs`) : `url`, `timeout`, **`headers` arbitraires** — donc notre propre `Authorization: Bearer …`, `max_response_size`.

⚠️ **La documentation web de Stalwart est une SPA non extractible** (`stalw.art/docs/` ne renvoie qu'un lien vers l'installation ; une douzaine d'URL plausibles pour les MTA Hooks renvoient 404). **Les éléments ci-dessus proviennent du code source et du CHANGELOG, pas d'une page de doc lisible.** À revérifier dans un navigateur avant implémentation.

⚠️ **Licence AGPL-3.0** : sans effet si on héberge pour soi ; si Pantry devient un service accessible à des tiers, l'AGPL §13 s'applique. Arbitrage à faire consciemment.

#### Postal — l'alternative MIT

<https://docs.postalserver.io/developer/http-payloads> — « Receiving e-mail by HTTP », form-data ou JSON au choix.
Format `processed` : `rcpt_to`, `mail_from`, `subject`, `message_id`, **`spam_status`**, `plain_body`, `html_body`, `attachments[]{filename, content_type, size, data}` (base64). Format `raw` : message base64 entier. Sépare automatiquement les citations et signatures. **Timeout 5 s, 18 tentatives en backoff exponentiel**, échec immédiat sur 5xx, et **bounce envoyé à l'expéditeur** en cas d'échec définitif. Aucune signature de webhook → URL secrète + HTTPS + filtrage réseau. Antispam intégrable (SpamAssassin, rspamd, ClamAV).

#### Avantages francs

- **Contrôle total sur le rejet.** C'est le problème n°1 (§1.2) : on décide de ne rien rejeter sur DMARC. Aucun mail de commande ne disparaît silencieusement.
- **RGPD trivial.** Les données ne quittent pas notre VPS. Pas de DPA, pas de TIA, pas de sous-traitant américain (voir §1.6).
- **Coût marginal.** Le VPS existe déjà.
- **Pas de plafond de règles ni de quota mensuel.**
- **Pas de problème de réputation sortante** — voir la nuance ci-dessous.

#### Inconvénients francs

- **Le port 25 entrant peut être bloqué chez l'hébergeur — à vérifier AVANT tout.**
  Hetzner : « *we block ports **25 and 465 by default on all cloud servers*** », déblocage possible après un mois d'ancienneté et paiement de la première facture, au cas par cas (<https://docs.hetzner.com/cloud/servers/faq>).
  DigitalOcean : « *SMTP ports **25, 465, and 587** are blocked on Droplets* » (<https://docs.digitalocean.com/support/why-is-smtp-blocked/>).
  ⚠️ **Ni l'un ni l'autre ne précise la DIRECTION du blocage.** L'usage veut que ce soit sortant, donc que la réception fonctionne, mais **ce n'est affirmé par aucune source officielle**. OVH et Scaleway : pages de politique non atteignables ce jour, **non vérifiable**.
  → **Action de 5 minutes avant toute décision** : `nc -l 25` sur le VPS cible et test de connexion depuis l'extérieur.
- **« Réception seule = pas de réputation à gérer » est vrai, avec deux nuances.**
  (a) **Les bounces nous transforment en émetteur.** Il faut **rejeter à la phase SMTP** (`RCPT TO` / `DATA`) plutôt qu'après acceptation : un rejet en session ne génère aucun mail sortant, alors qu'un rejet après acceptation oblige à émettre un NDR — avec risque de *backscatter* si l'expéditeur était falsifié. Cloudflare a d'ailleurs choisi la voie radicale : « *Non-delivery reports not forwarded to original senders* ».
  (b) On hérite quand même de la maintenance : TLS, mises à jour, antispam, anti-abus.
- **Surface à opérer** : parsing MIME, limites de taille, filtrage spam (rspamd/SpamAssassin), sauvegarde, supervision. Ce n'est pas énorme pour de la réception seule, mais ce n'est pas zéro.
- ⚠️ **Non vérifié faute de budget de recherche** : les exigences TLS réelles de Gmail/Outlook pour *délivrer* vers notre MX (STARTTLS obligatoire ou opportuniste ?), l'utilité de MTA-STS / DANE en réception, la nécessité d'un PTR en réception seule (il est requis pour émettre), le coût RAM/CPU de rspamd sur un petit VPS, et le volume de spam attendu sur une adresse à token aléatoire jamais publiée.

### 1.6 RGPD — l'état du droit a bougé, et ça compte dans le choix

- Le **Data Privacy Framework reste formellement valide** (décision d'adéquation UE 2023/1795), avec plus de 5 300 organisations américaines auto-certifiées.
- Le recours **Latombe** a été rejeté par le Tribunal de l'UE sur des motifs procéduraux ; **un pourvoi est pendant devant la CJUE** depuis octobre 2025, sans date d'audience annoncée.
- ⚠️ **Le 29 juin 2026, la Cour suprême des États-Unis a rendu l'arrêt *Trump v. Slaughter* (n° 25-332, 6-3)** : les restrictions empêchant le président de révoquer les commissaires de la FTC sont inconstitutionnelles. **L'indépendance de la FTC — l'un des piliers de l'adéquation — n'est plus garantie**, et le PCLOB fait face à la même objection. noyb demande une sortie immédiate du DPF. La recommandation des cabinets est de mettre à jour les *Transfer Impact Assessments* et d'« *evaluate whether EU-based or otherwise lower-risk alternatives are economically and technically viable* ».
  Source : <https://www.activemind.legal/guides/dpf-supreme-court/> (publié le 2 juillet 2026)

**Traduction pour Pantry** : des données de courses alimentaires nominatives, par foyer, sont des données personnelles révélatrices d'habitudes de vie (régime, allergies, convictions religieuses déductibles). Bâtir sur un fournisseur US en 2026 expose à une migration dans l'urgence si le pourvoi aboutit. Ce n'est pas un risque théorique cette année.

À noter également : traiter les mails d'une boîte, c'est traiter les données de **tiers qui n'ont jamais consenti**. Il faut recommander à l'utilisateur un **filtre de transfert sélectif** (expéditeur = enseigne), ne persister que les lignes extraites, et purger les emails bruts.

### 1.7 Recommandation pour le volet email

**Auto-hébergement avec Stalwart + MTA Hook**, avec **CloudMailin en repli managé**.

Stalwart règle simultanément les trois problèmes : le rejet DMARC (on décide de ne rien rejeter), le RGPD (rien ne quitte le VPS), et le coût (marginal). Le hook au stage `data` livre le message complet en JSON sur notre endpoint FastAPI, avec des headers d'authentification arbitraires et une réponse `accept`/`discard`/`reject`. Le projet est massivement actif.

*Conditions à lever avant de s'engager* : (a) tester le port 25 entrant chez l'hébergeur ; (b) lire la doc MTA Hooks dans un navigateur ; (c) arbitrer l'AGPL. **Si l'AGPL gêne, Postal (MIT) est un substitut direct**, payload très proche, 18 retries — au prix de l'absence de signature de webhook.

*Si l'auto-hébergement est refusé* : **CloudMailin** est le seul managé permettant de **forcer le traitement en région UE par DNS**, avec DPA art. 28. Réserves : basic auth seulement, 512 KB sur le gratuit (serré pour un mail HTML d'enseigne — le palier utile est Professional à 45 $/mois), et le sous-traitant OpenAI à clarifier. **ImprovMX Premium (9 $/mois, datacenters FR chez OVH)** est plus simple et moins cher, au prix d'une sécurité de webhook nulle (une seule IP à allowlister) et de 2 retries seulement.

**Règles de conception valables quel que soit le choix** :
- L'URL du webhook contient un secret long et aléatoire, en plus du mécanisme d'auth du fournisseur.
- **L'endpoint est idempotent**, clé `Message-ID` : les retries agressifs (Postmark 10, Postal 18, Mailtrap 10/24 h) garantissent des doublons.
- L'adresse par foyer est un **token aléatoire non devinable**, révocable et rotatable — c'est une capability, elle doit se traiter comme un secret.
- Prévoir un chemin de secours explicite quand le parsing échoue (Brevo a raison : 100 % est impossible).

---

## 2. Lecture directe de la boîte mail — voie alternative

### 2.1 Gmail API : le coût est réglementaire, pas technique

**`gmail.readonly` est bien un *restricted scope* en 2026.** La liste officielle des scopes restreints le confirme, et elle inclut aussi `gmail.metadata`, `gmail.modify` et `https://mail.google.com/` (ce dernier couvrant *tout* usage d'IMAP, SMTP et POP3).
Sources : <https://developers.google.com/workspace/gmail/api/auth/scopes> · <https://support.google.com/cloud/answer/13464325?hl=en>

**Il n'existe aucun repli moins sensible.** `gmail.metadata` est *aussi* restreint **et** ne donne pas le corps du message — donc inutile ici. Les scopes *sensitive* (non restreints) sont ceux des Workspace Add-ons, qui n'accordent qu'un accès **temporaire au message actuellement ouvert**, sans traitement en arrière-plan : l'utilisateur devrait ouvrir chaque mail et cliquer, ce qui détruit l'intérêt de l'automatisation.
Source : <https://developers.google.com/workspace/add-ons/concepts/workspace-scopes>
⚠️ **Incohérence documentaire relevée** : la page des scopes Gmail classe `gmail.addons.current.message.readonly` comme *sensitive*, la page Workspace add-ons le qualifie de *restricted*. Non tranché.

#### Préalable souvent fatal : le type d'application autorisé

Google exige que l'app appartienne à un type approuvé pour les scopes Gmail. Le n°4 est « *Applications that use information from emails to provide reporting or monitoring services for the benefit of users that **improve the email experience** (such as applications that automate travel itineraries or track flights or package delivery statuses)* ».
Source : <https://developers.google.com/workspace/workspace-api-user-data-developer-policy>

Pantry ressemble à ce pattern (extraction de récap depuis un mail), mais **une app de garde-manger améliore la gestion de stock, pas l'expérience email**. **C'est un risque de rejet réel, à l'appréciation de l'équipe Trust & Safety, et non chiffrable.**

#### Vérification OAuth

| Étape | Délai officiel |
|---|---|
| Brand verification | 2–3 jours ouvrés |
| Sensitive scope verification | 10 jours ouvrés |
| **Restricted scope verification** | **6 semaines** |

Source : <https://support.google.com/cloud/answer/13463817?hl=en>

Documents exigés (<https://support.google.com/cloud/answer/13464321?hl=en>) : homepage sur un domaine possédé et décrivant réellement l'app ; politique de confidentialité **sur le même domaine**, liée depuis la homepage *et* l'écran de consentement ; **propriété du domaine vérifiée via Search Console** ; **vidéo de démo** du flux OAuth complet, **en anglais**, avec le client ID visible dans la barre d'adresse ; justification par scope.

#### CASA — le point qui tue

**Toujours obligatoire en 2026** pour tout app à scope restreint ayant « *la capacité d'accéder aux données depuis ou via un serveur tiers* ».
Source : <https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification>

Gouvernance : le programme est porté par l'App Defense Alliance, **migrée sous la Joint Development Foundation (Linux Foundation)**, avec Google, Meta et Microsoft au comité de pilotage (<https://www.linuxfoundation.org/press/app-defense-alliance-migrates-under-jdf-with-google-meta-microsoft-as-steering-committee>). La nomenclature est passée de « Tier 2 / Tier 3 » à **Assurance Levels AL1 / AL2**, **imposés par Google** et non choisis par le développeur (<https://support.google.com/cloud/answer/13465431?hl=en> · <https://appdefensealliance.dev/casa/casa-tiering>).

🔴 **Le self-scan gratuit est mort.** « *The CASA self scanning process is **deprecated*** » (<https://appdefensealliance.dev/casa/tier-2/tier2-overview>) ; il ne subsiste que comme auto-évaluation avant le scan payant. **AL1 comme AL2 sont « Lab Tested – Lab Verified »** : passage obligatoire par un labo agréé. L'onboarding de nouveaux labos est par ailleurs **suspendu** suite à la migration — donc pas de pression concurrentielle à la baisse.

**Tarifs publics constatés le 3 août 2026** :

| Labo | Offre | Prix | Délai |
|---|---|---|---|
| TAC Security | **AL1 Basic** | **675 $** (barré 1 800 $) | 2–3 semaines |
| TAC Security | AL1 Premium | 855 $ | 2–3 semaines |
| TAC Security | AL2 Enterprise | 4 500 $/an | 2–4 semaines |
| Leviathan | AL1 « No Rush » | 3 000 $ | démarrage sous 30 j |
| Leviathan | AL1 « Priority » | 6 000 $ | démarrage sous 2 j |

Sources : <https://casa.tacsecurity.com/site/home> · <https://www.leviathansecurity.com/programs/google-casa-cloud-application-security-assessment> · liste des labos : <https://appdefensealliance.dev/casa/casa-assessors>

⚠️ **Deux chiffres à ne pas reprendre** : le « 15 000 – 75 000 $ » qui circule encore vient d'un [billet GMass de 2019/2020](https://www.gmass.co/blog/google-oauth-verification-security-assessment/) **antérieur au découpage en tiers** — obsolète. Et les grilles de blogs tiers (switchlabs, deepstrike) sont des compilations non officielles.

**Renouvellement : annuel, non négociable**, et « *l'évaluation annuelle CASA doit être un test complet de votre app, indépendamment de tout changement apporté* » — pas de tarif « renouvellement allégé ».
Source : <https://support.google.com/cloud/answer/13463816?hl=en>

**Témoignage direct, juillet 2026** : un développeur notifié par Google le 16 juillet 2026 pour une app perso à scope `drive` écrit « *the cost is ~540 $/year even at the cheapest, TAC Security. And it renews every 12 months* », « *The old free self-scan is gone; you must go through an accredited lab* ». **Il a abandonné son app** et s'est replié sur `drive.file`, non restreint. C'est le scénario de référence.
Source : <https://yurudeep.com/posts/aicoding/2026/20260717/en/>

⚠️ **L'échappatoire « pas de serveur » n'est pas confirmée.** L'annonce historique indiquait que les apps stockant les données uniquement sur l'appareil échappaient à l'évaluation complète. Un développeur a posé exactement cette question sur le forum officiel le 16 mars 2026 (<https://discuss.google.dev/t/is-casa-required-for-all-access-restricted-scopes/340650>) : **elle est restée sans réponse**. Aucune page officielle 2026 ne confirme la dispense. À traiter comme un pari.

#### La nuance « Testing » vs « non vérifiée » — elle change tout

Deux régimes que la plupart des sources confondent :

**Publishing status = « Testing »** (<https://support.google.com/cloud/answer/15549945?hl=en>) : 100 utilisateurs de test max, et surtout — « *A Google Cloud Platform project with an OAuth consent screen configured for an external user type and a publishing status of 'Testing' is issued **a refresh token expiring in 7 days*** » (<https://developers.google.com/identity/protocols/oauth2>). Reconnexion hebdomadaire : rédhibitoire.

**Publishing status = « In production » mais non vérifiée** : écran d'avertissement avant consentement, **plafond de 100 nouveaux utilisateurs cumulé sur toute la vie du projet, non réinitialisable** — mais **les refresh tokens n'expirent pas à 7 jours**. La règle des 7 jours est attachée au statut « Testing », pas à l'absence de vérification.

→ **C'est le seul chemin viable sans payer.** À noter que <https://support.google.com/cloud/answer/7454865?hl=en> énonce qu'on *doit* passer la vérification avant de lancer une app destinée aux utilisateurs : c'est toléré techniquement, pas béni contractuellement.

**Autres causes d'expiration de refresh token** (même page officielle), dont deux mordent ici :
- « *The user changed passwords and the refresh token contains Gmail scopes* » → **tout changement de mot de passe Google casse l'intégration**.
- Limite de **100 refresh tokens par compte Google et par client ID** ; au-delà, le plus ancien est invalidé silencieusement.
- Non-utilisation pendant 6 mois.

**Mode « Internal »** : exempte de vérification *et* du plafond, mais réservé aux membres d'une organisation Workspace/Cloud Identity. Un utilisateur externe reçoit `org_internal`. **Inapplicable à une app publique.**

#### Quotas Gmail API — non-sujet

6 000 unités/minute/utilisateur/projet, 80 000 000 unités/jour/projet avant facturation. `messages.list` = 5, `messages.get` = 20. Une synchro lisant 20 messages coûte ~405 unités : on pourrait faire ~200 000 synchros/jour dans le quota gratuit. **Les quotas ne seront jamais la contrainte — la vérification, si.**
Source : <https://developers.google.com/workspace/gmail/api/reference/quota>

### 2.2 IMAP chez les autres fournisseurs

| Fournisseur | Serveur | Auth 2026 | Source |
|---|---|---|---|
| **Gmail** | `imap.gmail.com:993` | **App password (2FA obligatoire)** ou OAuth — mais l'OAuth IMAP exige `https://mail.google.com/`, **restreint** | <https://support.google.com/mail/answer/185833?hl=en> · <https://developers.google.com/workspace/gmail/imap/xoauth2-protocol> |
| **Outlook.com perso** | `outlook.office365.com:993` | **OAuth 2.0 exclusivement** (auth basique retirée), scope délégué `https://outlook.office.com/IMAP.AccessAsUser.All` | <https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth> |
| **Microsoft 365** | idem | « *Basic authentication is now disabled in all tenants* » (page MàJ 16/07/2026) | <https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online> |
| **iCloud Mail** | `imap.mail.me.com:993` | **Mot de passe spécifique obligatoire**, 2FA requise, **max 25 actifs** | <https://support.apple.com/en-us/102654> · <https://support.apple.com/en-us/102525> |
| **Yahoo** | `imap.mail.yahoo.com:993` | App password si 2FA/Account Key | <https://help.yahoo.com/kb/SLN15241.html> |
| **Free.fr** | `imap.free.fr:993` | **Mot de passe principal du compte** — aucun app password | <https://assistance.free.fr/articles/609> |
| **Orange** | `imap.orange.fr:993` | Mot de passe dédié « logiciels de messagerie ». ⚠️ **POP/IMAP désactivés par défaut** sur les nouvelles boîtes — activation manuelle préalable | [assistance.orange.fr](https://assistance.orange.fr/ordinateurs-peripheriques/installer-et-utiliser/l-utilisation-du-mail-et-du-cloud/mail-orange/le-mail-orange-nouvelle-version/parametrer-la-boite-mail/mail-orange-comment-acceder-a-sa-boite-mail-orange-depuis-une-application-ou-un-logiciel-de-messagerie-non-fourni-par-orange_434630-964290) |
| **La Poste** | `imap.laposte.net:993` | Mot de passe principal ; TLS 1.2 min depuis juillet 2023 | [aide.laposte.net](https://aide.laposte.net/contents/comment-parametrer-laposte-net-sur-mon-logiciel-de-messagerie-suite-a-l-arret-des-protocoles-en-clair-non-cryptes) |

**Le point contre-intuitif** : sur Gmail, **IMAP+OAuth est plus lourd que l'API Gmail**, pas moins — Google écrit lui-même « *If your app doesn't require `https://mail.google.com/`, migrate to the Gmail API* ». Seul l'app password contourne tout.

**Le point favorable** : **Microsoft est le seul grand fournisseur où un dev solo peut faire les choses proprement et gratuitement.** OAuth délégué sur `IMAP.AccessAsUser.All` fonctionne pour Microsoft 365 **et** pour les comptes Outlook.com personnels ; il n'existe **aucun équivalent de CASA, aucun audit payant** ; et la *publisher verification* est **gratuite** (« *Microsoft doesn't charge developers for publisher verification* », <https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview>) et non bloquante pour des comptes personnels — elle exige toutefois un compte Microsoft AI Cloud Partner Program et une app enregistrée avec un compte work/school, pas un compte Microsoft personnel.

⚠️ **Non vérifiés** : la date exacte de fin de l'auth basique sur Outlook.com personnel (le 16 septembre 2024 revient de façon cohérente mais la page canonique reste vague) ; la date de bascule LSA pour les comptes @gmail.com personnels ; la limite de 25 mots de passe iCloud (pages Apple tronquées au fetch, corroborée par le forum développeur).

### 2.3 Le risque de stocker des mots de passe d'application

Un app password est un **secret réutilisable, longue durée, non révocable granulairement par notre app**, et qui donne souvent un accès **en écriture et en suppression** à toute la boîte — pas seulement en lecture. Une fuite de notre base = compromission totale des boîtes mail de tous nos utilisateurs, avec réinitialisation de mot de passe possible sur tous leurs autres services. Profil de risque très supérieur à un refresh token OAuth (scopé, révocable côté fournisseur).

Obligation RGPD (art. 32) : le hachage est inapplicable puisqu'il faut rejouer le secret. La CNIL admet le **chiffrement réversible** dans ce cas, mais **exige des mesures supplémentaires** — clé hors base (KMS/HSM ou secret d'environnement jamais versionné), rotation, journalisation des accès.
Source : <https://www.cnil.fr/fr/mots-de-passe-une-nouvelle-recommandation-pour-maitriser-sa-securite>

### 2.4 Conclusion tranchée

**Non, la lecture directe ne vaut pas le coup pour Pantry.**

L'arithmétique est brutale : 6 semaines de vérification, une homepage et un domaine vérifié en Search Console, une vidéo de démo en anglais, la nécessité de convaincre Google qu'une app de garde-manger « améliore l'expérience email », et **675 $ minimum tous les ans à perpétuité** pour un audit complet à chaque renouvellement — pour un projet sans revenu. Le self-scan gratuit qui rendait ça supportable n'existe plus.

**Si l'automatisation par lecture de boîte est absolument voulue malgré tout**, l'ordre est : (1) publier « In production » sans vérification et assumer le plafond de 100 utilisateurs à vie — les refresh tokens n'expirent pas dans ce régime, le coût est nul ; (2) commencer par **Microsoft**, pas par Google, si plusieurs fournisseurs doivent être couverts ; (3) IMAP + app passwords en dernier recours seulement, et seulement si l'on est prêt à traiter sa base comme un coffre-fort.

**La voie email entrant (§1) supprime entièrement ce problème** : aucune vérification d'aucun fournisseur, aucun secret d'utilisateur stocké, aucun accès à la boîte. C'est l'argument décisif en sa faveur.

---

## 3. Parsing des tickets de caisse par modèle multimodal

### 3.1 Coût — ce n'est pas le critère de décision, mais c'est le foyer qui paie

⚠️ **Cadrage imposé par les ADR-0005 et 0007** : Pantry ne choisit pas un modèle, il expose cinq adaptateurs et **chaque foyer configure le sien** (BYOK, Ollama local, ou la clé du propriétaire d'instance). Les chiffres ci-dessous ne servent donc **pas** à arbitrer une dépense d'exploitation — il n'y en a pas — mais à deux choses : (a) recommander un **défaut honnête** dans l'interface de sélection, puisque l'ADR-0007 note que « quatre fournisseurs à choisir, c'est aussi une charge de décision » ; (b) donner à l'utilisateur un ordre de grandeur de ce que son propre scan lui coûtera. Les repères sont donnés sur Claude parce que `claude-opus-5` est le défaut documenté de l'ADR-0005 ; ils se transposent aux autres adaptateurs.

**Comptage des tokens d'image chez Claude** : l'image est découpée en **patches de 28×28 px**, soit `⌈largeur/28⌉ × ⌈hauteur/28⌉` tokens visuels, avec double plafond (bord long **2576 px** et **4784 tokens** sur les modèles haute résolution ; **1568 px / 1568 tokens** sur palier standard comme Haiku 4.5). Au-delà, redimensionnement automatique.
Source : <https://platform.claude.com/docs/en/build-with-claude/vision>

Pour une image 1500×2000 px : `54 × 72 = 3888 tokens visuels`, sans redimensionnement (valeur confirmée dans le tableau officiel).
**Bonne nouvelle sur la forme d'un ticket** : un format très allongé coûte *moins* cher, car le plafond du bord long mord avant celui des tokens. 1000×3000 → redimensionné en 858×2576 → **2852 tokens**, contre 3888 pour du 1500×2000.

**Coût par ticket** (hypothèses : image 1500×2000, prompt 600 tokens, sortie JSON 800 tokens, une passe, pas de cache) :

| Option | Coût / ticket | 1 000 tickets |
|---|---|---|
| Claude Haiku 4.5 (palier standard, 1564 tokens image) | **0,6 ¢** | 6,16 $ |
| Claude Haiku 4.5 en Batch API (−50 %) | 0,3 ¢ | 3,08 $ |
| **Google Document AI / AWS Textract AnalyzeExpense / Azure prebuilt-receipt** | **1,0 ¢** | 10 $ |
| Claude Sonnet 5 (tarif intro jusqu'au 31/08/2026) | 1,7 ¢ | 16,98 $ |
| Claude Sonnet 5 (tarif standard) | 2,5 ¢ | 25,46 $ |
| Claude Opus 5 | 4,2 ¢ | 42,44 $ |
| Taggun | 4–6 ¢ | |
| Mindee | ≈ 5 ¢ (tarification ambiguë) | |
| Veryfi / Asprise | 8 ¢ (+ 500 $/mois de minimum chez Veryfi) | |
| Auto-hébergé (RTX 4090, > 50 k/mois) | ≈ 0,02 ¢ | + coût d'exploitation humain |

Sources : <https://aws.amazon.com/textract/pricing/> · <https://cloud.google.com/document-ai/pricing> · <https://azure.microsoft.com/en-us/pricing/details/document-intelligence/> · <https://www.veryfi.com/pricing/> · <https://www.taggun.io/pricing> · <https://www.mindee.com/pricing>

⚠️ **Correction d'une erreur qui circule** : plusieurs blogs traduisent le « $0.10 for every 10 pages » de Google en « 1 $ / 1000 pages ». C'est faux : 0,10 $ ÷ 10 = 0,01 $/page = **10 $/1000**. Le chiffre correct converge avec AWS et Azure au centime près, ce qui est un bon contrôle de cohérence.
⚠️ **Non vérifiés** : la page de tarifs Google Document AI n'a jamais chargé intégralement (chiffre issu d'un extrait pointant la page officielle) ; la page Azure affiche des placeholders `$-` (chiffre issu d'un consensus de sources secondaires) ; Mindee se contredit entre « 6 000 crédits par mois » et « par an » — écart de ×12 sur le prix/page, seul ancrage fiable = dépassement à partir de 0,05 $/crédit.

**Trois enseignements** :
1. **L'image représente ~87 % des tokens d'entrée.** Le prompt système est du bruit dans le budget.
2. **Le cache de prompt ne sert quasiment à rien ici** — l'image change à chaque ticket, et le préfixe stable (600 tokens) est sous le minimum cacheable de Sonnet 5 (1024 tokens). Ne pas architecturer autour du caching.
3. **L'écart entre les options viables (0,6 ¢ à 2,5 ¢) est négligeable devant le coût d'une erreur non détectée dans un stock alimentaire.** Le coût n'est pas le critère. La suite l'est.

### 3.2 Le vrai différenciateur : les libellés d'enseigne

Les parsers `receipt` de Google, AWS et Azure sont entraînés sur des tickets majoritairement anglophones et **retournent le libellé tel quel**. Ils ne savent pas que « PDT NOUV 1KG » est une pomme de terre nouvelle. Un modèle de langue le sait — c'est exactement ce que la connaissance du monde apporte. **Mais c'est aussi ce qui produit l'hallucination (§3.4).**

⚠️ **Constat de recherche, à connaître avant de planifier** :
- **Aucune source publique ne documente les abréviations de libellés produits des enseignes françaises** (Leclerc, Intermarché, Carrefour, Super U, Lidl, Aldi, Auchan). La longueur max de ~20–24 caractères découle du format d'impression thermique (58 mm ≈ 32 colonnes, 80 mm ≈ 42–48 colonnes) mais **aucune source ne documente la politique de troncature de ces enseignes**.
- **Aucun dataset public de tickets français n'existe.** La [collection French OCR datasets sur HF](https://huggingface.co/collections/lbourdois/french-ocr-datasets-67c8d3152330f11227e0d108) contient 3 datasets, tous de transcription générique. Aucun projet open source français de scan de tickets trouvé sur GitHub.

**Ce lexique est donc à la fois notre principal coût de démarrage et notre principal actif défendable.** Il n'existe nulle part et se construit empiriquement.

Datasets internationaux exploitables : **CORD** (1 000 tickets indonésiens, **30 entités hiérarchiques** sous `menu`/`subtotal`/`total` — la structure la plus proche de notre besoin), **SROIE** (1 000 tickets anglais, mais **4 champs seulement, pas de lignes de détail** — inutile ici), **CORU/ReceiptSense** (20 000 tickets arabe-anglais). Licences à vérifier individuellement avant usage commercial.
Sources : <https://rrc.cvc.uab.es/?ch=13> · <https://openreview.net/pdf?id=SJl3z659UH> · <https://arxiv.org/pdf/2406.04493>

### 3.3 Pièges physiques

**Papier thermique — la dégradation est chimiquement réversible.** Le mécanisme est un couple leuco-colorant + révélateur encapsulé : ce n'est pas un pigment fixé dans la fibre, c'est un mélange physique que rien ne verrouille.

| Grade | Lisibilité |
|---|---|
| **Économie (le plus courant en caisse)** | **7 à 30 jours** |
| Résistant huile/eau | 1–2 ans |
| Archival | 3–7 ans |

**L'accélérateur le plus destructeur est le contact avec huiles, plastifiants et solvants** — donc un **portefeuille PVC ou une pochette plastique**, exactement ce que font les gens qui « gardent leurs tickets pour les scanner plus tard ».
Sources : <https://www.ygtape.com/article/why-your-receipts-disappear> · <https://www.jotamachinery.com/academy/thermal-paper-fading/> · brevet <https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/9656498>
⚠️ Sources = blogs de fabricants (qui vendent le grade supérieur), mécanisme confirmé par la littérature brevets. **Aucune étude académique ne quantifie la chute du taux OCR en fonction de l'âge du ticket.**

→ **Conséquence produit directe : l'app doit pousser au scan immédiat**, pas offrir un confortable « mode rattrapage du week-end ».

**Angle, froissage, flou — l'angle est de loin le pire facteur.**

| Perturbation | Effet |
|---|---|
| **Angle de prise de vue** | précision **0,514 à 0° → 0,331 à 15° → 0,170 à 30°** — division par 3 |
| Inclinaison 5° | −3 à −8 % |
| Flou gaussien 0 → 1,5 | WER 0,24 → 0,34 |

⚠️ Chiffres issus de snippets, PDF source non extractible — **non vérifiés dans leur contexte méthodologique**.

→ **Recommandation : imposer un guide de cadrage visuel côté PWA et refuser la capture au-delà de ~10° d'angle détecté.** Le gain est probablement supérieur à celui d'un pipeline de *dewarping*. Si l'on veut quand même du dewarping, l'état de l'art le plus déployable est <https://arxiv.org/abs/2501.03145> (YOLOv8 + interpolation polynomiale cubique, CER 0,0235, meilleur que RectiNet/DocGeoNet/DocTr++ pour nettement moins de calcul).
⚠️ Les benchmarks de dewarping sont minuscules (DocUNet = 30 documents) ; toute annonce de SOTA dessus est statistiquement fragile. Et **personne n'a publié de comparaison « VLM avec vs sans dewarping sur tickets froissés »** — ReceiptBench reconnaît explicitement n'avoir mené aucune évaluation systématique d'augmentation visuelle.

**Tickets longs en plusieurs photos — territoire non défriché.** Attention au piège de vocabulaire : *multi-receipt detection* (N tickets distincts sur une photo — ce que font Mindee et Veryfi) n'est **pas** *long receipt capture* (1 ticket en N morceaux — notre problème). Presque toute la doc vendeur parle du premier.
- **Mindee** : le Multi-Receipt Detector isole plusieurs tickets d'une photo, et pour les PDF « chaque page est traitée comme une image séparée » → **ne recoud pas** (<https://www.mindee.com/blog/multi-receipt-detector-api>).
- **Veryfi** est le seul à revendiquer la fonctionnalité (« *automatically stitches together multiple pictures of a receipt in real time* »), mais la page « Detect, Crop & Stitch » est **« available per request »** — aucun paramètre d'API, aucune limite de pages, aucune spécification technique publiée.
- ⚠️ **Aucune publication académique ni article d'ingénierie sur le *receipt image stitching*.** Le stitching générique (SIFT/ORB + homographie) fonctionne mal sur du texte monospace répétitif à faible texture — précisément la nature d'un ticket. Aucune source non plus sur le tuilage avec chevauchement appliqué aux VLM sur tickets.

→ **Piste pragmatique, à valider empiriquement** : plutôt qu'un stitching pixel, envoyer les N photos **dans une seule requête** (jusqu'à 100 images ; au-delà de 20 images, redimensionner chacune à ≤ 2000 px de côté sous peine d'`invalid_request_error`), étiquetées « Image 1 : », « Image 2 : » comme le recommande la doc vision, avec instruction explicite de déduplication de la zone de recouvrement. On délègue le raccord au modèle plutôt qu'à OpenCV.

**Spécificités métier FR — ⚠️ non couvertes par cette recherche** : impression des produits au poids variable (prix/kg + quantité type 0,432 kg), remises et promos en lignes négatives, lots « 2+1 gratuit », « carte fidélité −X € », récapitulatif TVA multi-taux (5,5 / 10 / 20 %), consignes d'emballage, frais de sac. Pistes à explorer : article 289 du CGI, arrêté du 3 octobre 1983 (note remise au consommateur), fiche service-public « Facture et note : mentions obligatoires ».

**Un élément est en revanche déjà acquis, et il faut le réutiliser** : l'ADR-0008 a établi que les articles à **poids variable** portent des **codes internes magasin à préfixe `02` et `20`–`29`**, qui **embarquent le prix** — ils changent donc à chaque achat et ne figureront jamais dans un référentiel public. Côté scan, la décision est de les détecter côté client et de basculer en saisie manuelle sans appel réseau. **Côté ticket, la conséquence est différente et importante : une ligne au poids ne doit jamais être routée vers une recherche OFF par code, seulement vers le rapprochement par libellé, et de préférence vers le pivot Ciqual** (§3.6) puisqu'il s'agit typiquement de fruits, légumes, boucherie et vrac — précisément les catégories sans code-barres.

### 3.4 Le piège central : le modèle maquille l'arithmétique

**ReceiptBench (2026)** — 10 656 tickets réels, 19 champs, 4 tâches. Les scores par champ sont éloquents :

| Modèle | Global | Perception | Normalisation | Raisonnement | **Structure (lignes)** |
|---|---|---|---|---|---|
| **Qwen3-VL-8B (SFT+GRPO)** | 0,7950 | 0,8488 | 0,9416 | 0,8547 | **0,6373** |
| Gemini-3-Pro | 0,7373 | 0,7360 | 0,9086 | 0,8714 | **0,5781** |
| GPT-5 | 0,7076 | 0,7304 | 0,8743 | 0,8706 | **0,4893** |

Source : <https://arxiv.org/html/2605.22413v1>

**Le champ « lignes de détail » — exactement ce que nous voulons extraire — est de très loin le pire.** Les modèles frontier plafonnent entre 0,49 et 0,58 de F1, tandis qu'un 8B fine-tuné bat GPT-5 de +30 % relatif.
⚠️ **Composition linguistique : 98,0 % anglais, seulement 60 échantillons en français sur 10 656.** Les auteurs reconnaissent le biais anglo-centré. **Aucun benchmark public ne mesure sérieusement la performance sur tickets français.**

**Le comportement le plus dangereux**, nommé par les auteurs « *hallucination for arithmetic consistency* » :

> **Les modèles fabriquent ou modifient des lignes pour forcer la somme à correspondre au total imprimé.**

C'est directement notre question « le total ne tombe pas ». **Le modèle ne signale pas l'incohérence — il maquille les lignes pour la faire disparaître.** C'est le pire comportement possible : il transforme une erreur détectable en erreur silencieuse, **et il neutralise partiellement le contrôle arithmétique que nous comptions utiliser comme garde-fou.**

**Pourquoi c'est architectural et non corrigeable par prompt** — benchmark PP-OCRv6, taux de sorties sans contenu halluciné :

| Système | Précision anti-hallucination |
|---|---|
| **PP-OCRv6 medium** (OCR spécialisé, 34,5 M params) | **93,20 %** |
| Qwen3-VL-235B | 80,56 % |
| **GPT-5.5** | **78,00 %** |

Source : <https://arxiv.org/html/2606.13108> (Table 7)

Explication mécaniste des auteurs : les VLM « *ont tendance à corriger ce qu'ils perçoivent comme des fautes d'orthographe ou de grammaire dans l'image source, produisant un texte linguistiquement plausible mais factuellement incohérent avec l'entrée visuelle* », là où l'OCR spécialisé « *reproduit fidèlement le contenu exact — y compris les fautes délibérées — sans injecter de a priori linguistique* ».

**Traduction pour Pantry : un VLM face à « CRQ MONSIEUR X4 » subit une pression statistique à écrire « CROQUE MONSIEUR X4 ». Face à un chiffre partiellement effacé, il subit la même pression à produire le chiffre vraisemblable. C'est la même propriété qui nous fait gagner sur l'expansion des abréviations et perdre sur les montants. On ne peut pas avoir l'une sans l'autre.** D'où l'architecture hybride en §3.7.

**Contrepoint honnête** : sur les documents dégradés, les VLM restent nettement meilleurs que les moteurs classiques — CER 3 à 4× inférieur sur scans bruités et tickets ; sur factures scannées, Gemini 2.5 Pro 94 % et Claude 3.5 Sonnet 90 % contre AWS Textract 82 % et Tesseract 80–85 %.
⚠️ Chiffres agrégés de sources tierces par <https://parsli.co/blog/llm-ocr-vs-traditional-ocr>, non mesurés par eux, sur des modèles d'une génération en arrière.

### 3.5 Valider la sortie

**Sortie structurée garantie — GA chez Anthropic en 2026.** `output_config.format` avec `type: "json_schema"` procède par **échantillonnage contraint par grammaire compilée** : la sortie est *garantie* valide contre le schéma, pas validée après coup. `strict: true` fait l'équivalent sur les définitions d'outil.
Source : <https://platform.claude.com/docs/en/build-with-claude/structured-outputs>
(Le paramètre s'appelle bien `output_config.format` ; l'ancien `output_format` et le header beta `structured-outputs-2025-11-13` sont dépréciés.)

⚠️ **Le piège qui nous concerne directement** : `minimum`, `maximum`, `multipleOf`, `minLength`, `maxLength` **ne sont pas supportés**. **On ne peut donc pas contraindre un prix à être positif par une borne numérique.** `additionalProperties: false` est obligatoire sur tout objet. En revanche `enum` est supporté sur les nombres → **`{"type": "number", "enum": [5.5, 10, 20]}` est un contrainte valide pour les taux de TVA**. Pour tout le reste, les SDK transforment automatiquement le schéma (contrainte retirée, injectée en texte dans `description`, validation côté client) : **c'est exactement le pattern Pydantic, à assumer côté FastAPI.**

Autres points opérationnels : compilation de grammaire au premier appel puis **cache 24 h** ; incompatible avec les citations et le prefill ; **vérifier `stop_reason` AVANT de parser** (`"refusal"` ou `"max_tokens"` → la sortie peut ne pas respecter le schéma).

**Contrôles arithmétiques** — utiles, mais **pas suffisants** au vu de §3.4 :
1. `Σ(prix_ligne) == sous_total`
2. `sous_total + TVA − remises == total_imprimé`
3. `Σ(base_HT par taux × taux) == TVA totale`
4. Lignes au poids : `quantité × prix_unitaire == prix_ligne` (±1 centime)
5. Cohérence des taux : produit alimentaire → 5,5 % attendu

| Écart | Action |
|---|---|
| 0 | Confiance relative — **mais pas de validation automatique aveugle** (cf. maquillage) |
| ±1 à 3 centimes | Arrondi TVA, tolérer |
| Écart = prix exact d'une ligne | Ligne dupliquée ou manquante → relecture ciblée |
| Quelconque | Écran de revue humaine, ligne par ligne |

**Principe** : **valider contre l'image, pas contre la cohérence interne de la sortie.** Le contrôle arithmétique reste un détecteur d'échec franc, pas un certificat de justesse.

**Score de confiance — l'API Claude n'expose pas de logprobs.** Vérifié sur la référence complète de la Messages API : aucun `logprobs`, aucun `top_logprobs`, aucun score par token, ni en entrée ni en sortie.
Source : <https://platform.claude.com/docs/en/api/messages> (demande communautaire ancienne et non satisfaite, cf. <https://github.com/anerli/anthropic-logprobs>)
→ **Conséquence d'architecture, renforcée par l'ADR-0005** : non seulement Anthropic n'expose pas de logprobs, mais **le port `ModelProvider` ne peut de toute façon pas dépendre d'une fonctionnalité propre à un adaptateur**. Le signal de confiance doit donc vivre **au-dessus du port**, dans le domaine, et fonctionner identiquement que le foyer soit sur Claude, Mistral ou un petit modèle Ollama. C'est un argument décisif en faveur de la corroboration croisée ci-dessous : elle est la seule technique qui ne demande rien au fournisseur.

Même remarque pour la sortie structurée : `output_config.format` est spécifique à Anthropic. L'ADR-0005 prévoit la **dégradation par capacités détectées** — le domaine doit donc traiter « schéma garanti par grammaire » comme un *bonus* et non comme un invariant, et la validation Pydantic côté serveur reste obligatoire dans tous les cas.

Alternatives, par rapport coût/signal :

| Technique | Coût | Fiabilité |
|---|---|---|
| **Corroboration croisée VLM × OCR classique** | +ε (CPU) | **Le plus adapté à notre problème** — voir ci-dessous |
| Auto-consistance N=2 | ×2 | Très bon rapport : *Two Samples Are Enough* montre que 2 échantillons suffisent à une estimation robuste (<https://openreview.net/forum?id=66D3rZrNjV>) |
| Confiance verbalisée seule | ×1 | ⚠️ **Mal calibrée sans entraînement** (<https://arxiv.org/pdf/2603.17839>). **Ne pas utiliser seule.** |

**La corroboration croisée est directement justifiée par le mécanisme de §3.4** : l'OCR classique (PP-OCRv6, PaddleOCR, docTR) est fidèle au pixel et **ne réécrit pas**, mais ne structure pas ; le VLM structure et expanse les abréviations, mais réécrit. **Faire tourner les deux en parallèle et signaler les divergences numériques transforme deux faiblesses complémentaires en détecteur d'erreur.** Coût quasi nul (CPU).
⚠️ **Aucun papier ne publie cette architecture appliquée aux tickets** — c'est un raisonnement dérivé des sources, pas une recommandation sourcée.

**Écran de revue humaine.** Mindee est le plus explicite et reconnaît frontalement le problème (« *L'encre des imprimantes thermiques se dégrade rapidement* ») : **score de confiance par champ** sur une échelle Low/High/Certain, **routage conditionnel** (écriture auto quand certain, opérateur humain pour les documents endommagés), et **mémorisation des corrections** appliquées instantanément aux documents similaires.
Source : <https://www.mindee.com/blog/receipt-data-extraction-ai-guide>
(Veryfi annonce 97 % de précision : ⚠️ chiffre commercial non audité, sans définition publique de la métrique.)

**Ce que notre écran de revue doit montrer** :
1. La photo du ticket **à côté** du JSON, avec surlignage de la zone source de chaque ligne quand le modèle sait rendre des bounding boxes (coordonnées absolues, approximatives — <https://platform.claude.com/docs/en/build-with-claude/vision-coordinates>)
2. Le résultat du contrôle arithmétique, en vert/rouge, **avec l'écart chiffré**
3. **Les lignes en divergence VLM ↔ OCR, en premier**
4. Les libellés non appariés à une fiche produit, groupés
5. **Zéro pré-validation par défaut au départ** : le premier ticket d'une enseigne se valide intégralement ; les suivants profitent de la table d'alias.

### 3.6 Rapprochement libellé → fiche produit

> **Cette sous-section se raccorde à l'existant, elle ne le rejoue pas.** La stratégie Open Food Facts est déjà tranchée par [ADR-0008](adr/0008-open-food-facts-integration.md) et instruite par [`technical-notes-scanning.md`](technical-notes-scanning.md) : cache d'abord (condition de fonctionnement, pas optimisation), catalogue partagé matérialisé par `household_id IS NULL`, API **v3** (la v2 est dépréciée, contrat d'erreur différent), `User-Agent` honnête, environnement de recette `world.openfoodfacts.net` en développement, et **import du dump local en prérequis dès la phase 2**. Ce qui suit ne fait qu'ajouter ce que le rapprochement *libellé de ticket → fiche produit* impose en plus du scan EAN.

#### Open Food Facts — l'API en ligne est inutilisable, le dump l'est

**Rate limits officiels, et ils sont durs** :

| Opération | Limite |
|---|---|
| Lecture produit | **15 requêtes / min / IP** |
| **Recherche** | **10 requêtes / min / IP** |
| Écriture | aucune |

**User-Agent personnalisé obligatoire** (`AppName/Version (ContactEmail)`), et OFF « *se réserve le droit de refuser l'accès par bannissement d'adresse IP* ».
Sources : <https://openfoodfacts.github.io/openfoodfacts-server/api/> · <https://support.openfoodfacts.org/help/en-gb/12-api-data-reuse/94-are-there-conditions-to-use-the-api>

→ **10 recherches/min ≈ 1 ticket de 10 lignes par minute. Le design correct est un dump local en base, l'API ne servant que de fallback pour les codes-barres inconnus.** Ce n'est pas négociable.

**Et cela avance l'échéance fixée par l'ADR-0008.** Celui-ci reporte l'import du dump à la phase 2, au motif que le plafond (~15 req/min en lecture produit) reste tenable en phase 1 pour du scan EAN. **Le rapprochement de libellés ne relève pas du même plafond** : il consomme l'endpoint de *recherche*, plafonné à **10 req/min**, et il émet une requête **par ligne de ticket** au lieu d'une par scan. Un seul ticket de courses sature donc la minute. **Conséquence : dès que l'ingestion par ticket entre en service, le dump local devient un prérequis de phase 1, pas de phase 2.** C'est le principal impact de cette note sur les décisions déjà prises, et il mérite un amendement d'ADR-0008.

Autre subtilité : **la recherche plein texte n'existe pas dans l'API v2** (`/api/v2/search` est une recherche *structurée* sur `categories_tags`/`brands_tags`/`code`). La recherche par nom est migrée vers **Search-a-licious** (backend Elasticsearch, `search.openfoodfacts.org`, `q` acceptant la syntaxe Lucene — utile pour filtrer par catégorie/marque tout en laissant les tokens libres partir en full-text).

**Volumes au 3 août 2026** : 4,72 M produits toutes bases ; **1 255 083 pour la France** (~28 % du catalogue mondial, le meilleur ratio de couverture nationale). Filtrer `countries_tags: en:france` met la table dans la zone confortable de `pg_trgm`.
Source : <https://fr.openfoodfacts.org/>

**Exports** : préférer le **Parquet** (>150 colonnes, schéma typé, chargeable via DuckDB ou pyarrow → `COPY` binaire) au CSV (~0,9 Go compressé / ~9 Go décompressé, et un champ de mines : quoting, retours ligne dans les noms, colonnes qui bougent). Miroir français ODbL sur data.gouv.fr, Parquet mis à jour le 2 août 2026.
Sources : <https://world.openfoodfacts.org/data> · <https://www.data.gouv.fr/datasets/open-food-facts-produits-alimentaires-ingredients-nutrition-labels>

**Licence ODbL — les implications concrètes** :

| Objet | Licence |
|---|---|
| **Structure** de la base | **ODbL 1.0** |
| Contenus individuels | DbCL 1.0 |
| Photos produits | CC-BY-SA 3.0 |

Source : <https://world.openfoodfacts.org/terms-of-use>

La distinction qui décide de tout : **notre copie locale enrichie est une *Derivative Database*** (la stocker n'oblige à rien tant qu'on ne la distribue pas publiquement ; dès qu'on la publie, elle repart sous ODbL, enrichissements inclus), tandis que **notre PWA, nos écrans et nos exports sont un *Produced Work*** — **l'ODbL ne contamine pas le code de Pantry**, seule la **notice d'attribution** avec lien vers openfoodfacts.org est due dès qu'il y a diffusion publique. Les marques et le droit à l'image sur les emballages ne sont **pas** concédés par OFF.

→ **Conseil d'architecture juridique : garder la table d'alias apprises dans une table séparée référençant les codes-barres, plutôt qu'en colonnes ajoutées à la copie OFF.** Ça garde la frontière ODbL lisible si le service s'ouvre un jour.

**Limites de qualité pour le matching par nom** : `product_name` est saisi par des contributeurs sans schéma et mélange marque, dénomination, parfum et grammage (« Nutella » / « Nutella 400g » / « Pâte à tartiner Nutella ») ; **`product_name_fr` n'est pas garanti rempli** sur une fiche française (`COALESCE` obligatoire) ; `quantity` est du texte libre. Une requête « lait demi-écrémé » renvoie des milliers de fiches quasi identiques — **le problème n'est pas le rappel, c'est la précision.**
⚠️ **Le taux de complétion de `product_name_fr` / `brands` / `quantity` sur le sous-ensemble France n'est publié nulle part.** C'est le chiffre le plus important pour ce volet, il se calcule en dix minutes de SQL sur le dump, et il détermine si l'approche tient debout. **À mesurer avant d'écrire une ligne de code.**

**Deux sources complémentaires** :
- **Open Prices** (<https://prices.openfoodfacts.org>) — 285 467 prix et 112 637 preuves au 3/08/2026. Faible comme catalogue (~6 % de couverture), **mais fort comme source de vérité sur les libellés d'enseigne** : les preuves sont des **photos de tickets**, avec association ticket ↔ code-barres. **C'est le seul gisement public identifié contenant à la fois un libellé de caisse français et un GTIN.** À creuser sérieusement pour amorcer le lexique.
- **Ciqual / ANSES** (<https://www.anses.fr/en/content/ciqual-nutritional-composition-table>) — 3 484 aliments, **Licence Ouverte / Etalab** (réutilisation commerciale libre + attribution). Inutile comme catalogue produits, **précieux comme référentiel d'aliments génériques** : « PDT NOUV 1KG » n'a **aucune fiche OFF possible** (pas de code-barres) mais a une entrée Ciqual évidente.
→ **Architecture à deux catalogues : OFF pour l'emballé, Ciqual pour le frais et le vrac.**

#### Le fossé de registre — le vrai point dur

**« PDT NOUV 1KG » et « Pommes de terre nouvelles de Noirmoutier, 1 kg » ne partagent presque aucun trigramme et aucun lexème après stemming.** Aucun moteur de similarité de chaînes ne rattrapera ça, quel que soit le moteur. **Il faut une couche d'expansion d'abréviations en amont** (PDT→pomme de terre, CRQ→croque, NOUV→nouvelle, LT DEM 1/2 ECR→lait demi-écrémé), **puis** seulement de la recherche. C'est là que se joue le taux de réussite, pas dans le choix HNSW vs IVFFlat.

Littérature la plus proche : **Gorman, Kirov, Roark & Sproat, « Structured abbreviation expansion in context »** (<https://arxiv.org/abs/2110.01140>) traite exactement des abréviations *ad hoc*, intentionnelles, s'écartant substantiellement du mot d'origine et résolues par le contexte — littéralement « PDT NOUV ». Et **Tomanek, Cai & Venugopalan** (<https://arxiv.org/abs/2312.14327>) traitent la personnalisation avec très peu de données utilisateur, transposable à une boucle d'apprentissage par foyer.
⚠️ **Aucun corpus ni projet open source dédié aux abréviations de tickets de caisse français.**

#### Repères PostgreSQL

**`pg_trgm`** (contrib standard, PG 16/17/18) — **le point décisif : utiliser `word_similarity` (`<%` / `%>`, seuil 0.6), pas `similarity` (`%`, seuil 0.3)**. `similarity('CRQ MONSIEUR X4', 'Croque-monsieur jambon fromage')` sera catastrophique (le dénominateur trigrammes écrase le score), tandis que `word_similarity` cherche la meilleure sous-séquence continue de la seconde chaîne — exactement la forme du problème (libellé court à retrouver *dans* un nom long).
⚠️ `set_limit()` / `show_limit()` sont dépréciées → `SET pg_trgm.similarity_threshold`.
**GIN pour le pré-filtre** (1,25 M → quelques centaines de candidats) puis tri applicatif ; GiST n'est nécessaire que pour du KNN `ORDER BY col <-> 'txt' LIMIT n`.
⚠️ **Aucun benchmark fiable de pg_trgm sur 50 k – 2 M lignes n'a été trouvé.** Le risque documenté est le seuil trop bas : à 0.1 sur 1 M lignes, les candidats explosent et le planner bascule en seq scan. À mesurer.
Source : <https://www.postgresql.org/docs/18/pgtrgm.html>

**`unaccent` — le piège de l'IMMUTABLE.** `unaccent()` est `STABLE`, donc `CREATE INDEX ... (unaccent(nom))` échoue. Le wrapper correct nomme explicitement le dictionnaire :

```sql
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text AS
$$ SELECT public.unaccent('public.unaccent', $1) $$
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT;

CREATE INDEX idx_produits_nom_trgm
  ON produits USING gin (f_unaccent(lower(product_name)) gin_trgm_ops);
```

⚠️ L'index n'est utilisé que si la requête réécrit **exactement** la même expression, même ordre.

**FTS française** : configuration `french` standard, à combiner avec unaccent comme dictionnaire *filtrant* (avant le stemmer). `websearch_to_tsquery` est le seul parser qui **ne lève jamais d'erreur de syntaxe** → le seul sûr sur une entrée brute. `ts_rank_cd` (cover density, tient compte de la proximité) plutôt que `ts_rank`. **PostgreSQL 18 n'apporte aucune nouveauté FTS et toujours pas de BM25 en core.**

**BM25** : `pg_search` de ParadeDB (v0.25.0 du 28/07/2026, 9,1 k ★, releases hebdomadaires, PG 15+) est mature — ⚠️ **mais AGPL-3.0** : sans effet en auto-hébergement pur, l'§13 s'applique si Pantry devient un service accessible à des tiers. **Arbitrage à faire consciemment, pas à découvrir plus tard.**

**`pgvector` — épingler ≥ 0.8.6** (publiée le 29 juillet 2026). Historique récent : **six correctifs en cinq mois, dont une corruption d'index HNSW au vacuum (0.8.3) et deux buffer overflows**. Ce n'est pas un motif de rejet (le projet corrige vite et publiquement), mais ne pas rester sur une 0.8.x antérieure.
Source : <https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md>

**Embeddings** : **Qwen3-Embedding-0.6B** (Apache 2.0, MTEB multilingual 64,33) — le **MRL permet de tronquer à 256 ou 384 dims sans réentraîner**, ce qui divise par 3–4 la taille de l'index HNSW ; sur des libellés de 3–6 tokens, 1024 dims est du gaspillage. Alternative : **BGE-M3** (MIT), qui produit **dense + sparse dans le même forward pass** → les deux jambes de la fusion sans extension BM25 AGPL. Pour du français pur, **Solon-embeddings-large-0.1** (MIT) a les seuls chiffres FR publiés (MTEB-FR 0,7490).
⚠️ **Aucun de ces benchmarks n'évalue du libellé abrégé.** Prévoir un jeu d'évaluation maison de 200–300 paires annotées : c'est le seul chiffre qui comptera.

**Fusion hybride — RRF, k=60** : `score(d) = Σ_r 1/(k + rank_r(d))`. Le principe qui fait que ça marche : on ne fusionne pas des **scores** (incomparables entre BM25 et cosinus) mais des **rangs**, ce qui rend la fusion insensible à la calibration de chaque moteur. Implémentation PostgreSQL de référence : <https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py> — deux CTE, rang par window function, **`FULL OUTER JOIN`** + `COALESCE` (détail qui compte : un document trouvé par un seul moteur reste candidat).
→ **Ajouter une troisième jambe trigramme dans la même fusion : sur des libellés abrégés, le trigramme rattrape ce que le stemmer français et l'embedding ratent tous les deux — les troncations (`CRQ`, `PDT`) qui ne sont ni des lexèmes ni sémantiquement porteuses.**
⚠️ La valeur k=60 est confirmée par l'implémentation pgvector, pas par le papier source (Cormack et al., SIGIR 2009, non récupérable).

**`RapidFuzz`** (MIT, cœur C++) en **re-ranking sur 50–200 candidats déjà sortis de PostgreSQL, jamais en scan complet** ; `token_set_ratio` quand le libellé est un sous-ensemble désordonné du nom produit.

#### Boucle d'apprentissage

⚠️ **Cette sous-section n'est pas sourcée** — c'est du raisonnement d'ingénierie appuyé sur les papiers cités.

```sql
CREATE TABLE receipt_alias (
    id               bigserial PRIMARY KEY,
    raw_label        text NOT NULL,          -- libellé brut, tel qu'imprimé
    normalized_label text NOT NULL,          -- lower + unaccent + espaces normalisés
    retailer         text,                   -- les abréviations sont propres à l'enseigne
    product_code     text,                   -- GTIN Open Food Facts, nullable
    ciqual_code      text,                   -- pivot frais/vrac, nullable
    confirmed_by     text NOT NULL,
    confirmed_at     timestamptz NOT NULL DEFAULT now(),
    hit_count        integer NOT NULL DEFAULT 0,
    last_hit_at      timestamptz,
    CONSTRAINT one_target CHECK (num_nonnulls(product_code, ciqual_code) = 1)
);
CREATE UNIQUE INDEX ON receipt_alias (normalized_label, coalesce(retailer, ''));
```

Quatre points non négociables :

1. **La clé est (libellé normalisé, enseigne)**, pas le libellé seul. « CRQ » chez Leclerc et chez Carrefour ne dénotent pas forcément la même chose, et **un alias faux appris globalement empoisonne tout le corpus**.
2. **Garder le libellé brut ET normalisé** : le jour où la fonction de normalisation change, il faut pouvoir rejouer sur le brut.
3. **Consigner aussi les rejets.** L'utilisateur qui refuse la proposition n°1 et choisit la n°3 produit un signal négatif — exactement le « hard negative » de Block-SCL (<https://arxiv.org/abs/2207.02008>), **et le signal le plus cher à obtenir**. Une table qui n'enregistre que les succès jette la moitié de l'information.
4. **Le lookup d'alias court-circuite tout le pipeline** : un `SELECT ... WHERE normalized_label = ? AND retailer = ?` en O(1) **avant** de toucher pg_trgm ou l'embedding. Après quelques dizaines de courses, l'essentiel du panier récurrent d'un foyer est couvert, et le pipeline coûteux ne sert plus qu'aux nouveautés. **C'est ce qui rend le projet viable économiquement.**

Et le dictionnaire d'abréviations s'auto-alimente : chaque alias confirmé donne un alignement libellé ↔ nom produit, dont on extrait les correspondances token à token. **Notre boucle d'apprentissage réelle n'est pas du fine-tuning de modèle, c'est de l'accumulation de dictionnaire.**

### 3.7 Architecture proposée pour le volet ticket

```
PWA (client)
  └─ guide de cadrage, rejet si angle > 10°, downscale contrôlé
     │
     ├─► [1] OCR classique (PP-OCRv6 / docTR, CPU, ~0 €)
     │        └─ texte fidèle au pixel, non structuré
     │
     └─► [2] VLM (Claude Sonnet 5, ou modèle auto-hébergé)
              └─ output_config.format + JSON Schema → lignes structurées
     │
  [3] CORROBORATION : divergences numériques [1] × [2] → score de confiance
     │
  [4] Validation Pydantic + contrôles arithmétiques
     │   (⚠️ détecteur d'échec franc, PAS certificat de justesse — cf. §3.4)
     │
  [5] Matching libellé → produit
     ├─ lookup receipt_alias (normalized_label, retailer)   ← O(1), court-circuit
     ├─ expansion d'abréviations (dictionnaire auto-alimenté)
     ├─ blocking pg_trgm word_similarity sur f_unaccent(lower(...)) → ~200 candidats
     └─ RRF k=60 sur 3 jambes : trigramme + FTS fr_unaccent + vecteur (Qwen3-0.6B @256d)
     │
  [6] Écran de revue humaine (photo + JSON + écarts + divergences)
     │
  [7] Écriture en base + apprentissage (alias confirmés ET rejetés)
```

**Défaut recommandé dans l'interface de sélection (ADR-0005/0007) : Claude Sonnet 5** pour l'extraction de ticket, avec Haiku 4.5 évalué en parallèle — il est structurellement avantagé côté coût car sur palier standard (1564 tokens là où Sonnet en paie 3888), mais il voit une image redimensionnée à 1269×952, ce qui peut être rédhibitoire sur un ticket. Ce n'est **pas** un choix d'architecture : c'est la valeur que l'interface propose par défaut, et que le foyer reste libre de changer.

⚠️ **Point de vigilance sur la dégradation par capacités.** L'ADR-0007 anticipe déjà que « l'extraction de ticket sur papier thermique abîmé fonctionne bien avec un modèle propriétaire récent, médiocrement avec un petit modèle ouvert ». Les chiffres de §3.4 le confirment et l'aggravent : **même les modèles frontier plafonnent à 0,49–0,58 de F1 sur les lignes de détail**. Il faut donc afficher une attente honnête dès la configuration, et **ne jamais désactiver l'écran de revue humaine en fonction du fournisseur** — la tentation de « faire confiance à Opus et pas à Ollama » est exactement le raccourci que le maquillage arithmétique rend dangereux.

**Chemin de démarrage** :
1. Constituer **200–500 tickets français annotés** (enseigne, date, total, TVA par taux, lignes) en incluant délibérément du froissé, du thermique décoloré, du photographié de travers.
2. Mesurer le **taux de complétion des champs OFF France** (10 min de SQL) — ça conditionne toute la stratégie de matching.
3. Comparer sur ce corpus : Claude Sonnet 5, Claude Haiku 4.5, Google Expense Parser (1 ¢), et PaddleOCR-VL-1.6 / LightOnOCR-2-1B en local.
4. **Mesurer au niveau champ** (exact match sur le total, F1 sur les lignes), **pas au niveau caractère**. Un CER de 2 % qui tombe sur le chiffre des centimes est un échec métier.
5. Explorer Open Prices comme amorce de lexique libellé-caisse ↔ GTIN.

**Note sur l'auto-hébergement** : le paysage a basculé en 2026 — sur OmniDocBench v1.6, **PaddleOCR-VL-1.6 (0,9 B, Apache 2.0) obtient 96,34, devant Gemini 3 Pro (92,91) et Qwen3-VL-235B (89,78)**. Les modèles pertinents pèsent 2 à 3,5 Go en fp16 : une RTX 4090 24 Go suffit largement. Deux candidats à surveiller pour le français : **PaddleOCR-VL 1.6** (109 langues, français nommé) et **LightOnOCR-2-1B** (Apache 2.0, français, entraînement ciblant explicitement les « *scans, French documents* », 5,71 pages/s sur une H100 — mais **Tables 45,4**, faible, à tester sérieusement si l'on extrait les lignes en tabulaire).
⚠️ **Licences bloquantes à connaître** : Nanonets-OCR (Qwen Research License, **usage commercial interdit**), Surya (poids non commerciaux au-delà d'un seuil de CA), Qwen2.5-VL (le 7B est Apache 2.0, **le 3B et le 72B ne le sont pas**), MinerU (addendum possible). Florence-2 est à écarter (score 0 en zero-shot sur DocVQA).
Source : <https://github.com/opendatalab/OmniDocBench>

**Ces modèles ouverts sont la vraie substance du mode `ollama` de l'ADR-0007.** Celui-ci décrit l'inférence locale comme un mode de premier rang, mais la qualité y était une inconnue. Le classement OmniDocBench la lève partiellement : un modèle **Apache 2.0 de 0,9 B** tenant sur n'importe quel GPU grand public bat Gemini 3 Pro sur le parsing documentaire. **Pour l'extraction de ticket spécifiquement, le mode local n'est donc pas le parent pauvre** — il l'est en revanche pour les suggestions de recettes, qui demandent du raisonnement généraliste. Cette asymétrie mérite d'être dite à l'utilisateur au moment de la configuration, plutôt que de laisser croire à une dégradation uniforme.

**Seuil de bascule vers l'auto-hébergement** : sous 50 000 tickets/mois, l'API cloud reste plus rentable (le temps d'ingénierie coûte plus que l'écart). Au-delà de 350 000/mois, l'auto-hébergement gagne d'un facteur 4 à 10. **À toute volumétrie si l'argument est RGPD/souveraineté** — un ticket de caisse est une donnée personnelle révélatrice (habitudes de consommation, géolocalisation implicite). C'est exactement la logique déjà retenue par l'ADR-0007, qui met en avant `ollama` et `byok` Mistral (hébergé UE) comme les deux configurations gardant les données sous juridiction européenne.

---

## 4. Export de la liste de courses

### 4.1 Le résultat qui change l'architecture

La question « comment écrire dans les Rappels iCloud ? » est mal posée, et c'est ce qui piège tout le monde. Il faut séparer deux choses que le web confond systématiquement :

| | État 2026 |
|---|---|
| **iCloud comme serveur**, exposant *ses* Rappels en CalDAV à un tiers | ❌ **Mort depuis iOS 13** |
| **L'app Rappels comme client CalDAV d'un serveur tiers** (le nôtre) | ✅ **Fonctionne, prouvé en avril 2026 sur iOS 26.4.1** |

**N'essayons pas d'écrire dans iCloud. Soyons le serveur CalDAV.**

### 4.2 Rappels iCloud — trois impasses et une ouverture

**Aucune API publique Apple.** EventKit est strictement local (framework on-device pour app native signée, surveillant la base Calendar de l'appareil — <https://developer.apple.com/documentation/eventkit>). CloudKit Web Services ne donne accès qu'à **nos propres conteneurs** : la structure d'URL est `https://api.apple-cloudkit.com/database/1/[container]/...` où « *The container ID begins with `iCloud.`* » et se crée dans **notre** compte développeur. **Aucun mécanisme documenté ne permet de cibler le conteneur d'une autre app**, a fortiori une app système Apple.
Source : <https://developer.apple.com/library/archive/documentation/DataManagement/Conceptual/CloudKitWebServicesReference/SettingUpWebServices.html>

**Le CalDAV d'iCloud n'expose plus les Rappels.** C'était votre question, et la réponse est sourcée :

> « *Reminders sync has been **disabled by Apple** and is only available when you use very old iOS versions and never upgraded it.* »
> — DAVx⁵, client CalDAV de référence sur Android : <https://www.davx5.com/tested-with/icloud>

Corroborations convergentes :
- **Tasks.org** : « *The new Apple Reminders app introduced in iOS 13 and macOS 10.15 uses a proprietary format that is not compatible with Tasks* » (<https://tasks.org/docs/caldav_icloud.html>)
- **BusyMac** : à l'upgrade iOS 13/Catalina, « *the new Reminders app migrates all your to-do-only calendars off of CalDAV and into a **private silo that only the Apple Reminders app can access*** » (<https://www.busymac.com/docs/faqs/112990-reminders-in-ios-13-and-macos-catalina-drops-support-for-caldav/>)
- **python-caldav** — source primaire parlante : le profil iCloud dans `compatibility_hints.py` est **commenté/désactivé** et porte le drapeau **`'no_todo'`**. L'issue de référence est **close depuis mars 2021** : « *I will close this issue, as no more work is planned to be done on icloud support* » (<https://github.com/python-caldav/caldav/issues/3>)
- **Home Assistant** : les listes de rappels iCloud remontent comme collections CalDAV mais « *never show events* » ; le ticket de février 2025 signalant qu'elles arrivent « *with a warning and do not have the correct content* » a été **fermé en "not planned"** (<https://github.com/home-assistant/core/issues/138121>)

⚠️ **Contre-indice signalé par honnêteté** : un billet technique montre une config vdirsyncer avec `item_types = ["VTODO"]` contre `caldav.icloud.com`, tout en précisant que « *the built-in iCloud integration for Reminders and Calendars doesn't use the same CalDav endpoint* » (<https://heywoodlh.io/cross-platform-icloud/>). Lecture la plus cohérente : des VTODO existent bien côté serveur mais dans un **silo parallèle invisible de l'app Rappels native**. Non tranché sans test sur compte réel — **ne rien bâtir dessus**.

**L'authentification est de toute façon hostile.** Mot de passe d'application obligatoire, généré sur **`account.apple.com`** (plus `appleid.apple.com`), **2FA requise**, **max 25 actifs**, et surtout : « *Any time you change or reset your primary Apple Account password, **all of your app-specific passwords are revoked automatically***. » Les services couverts sont énumérés comme « *mail, contacts, and calendars* » — **les Rappels n'y sont jamais mentionnés**.
Source : <https://support.apple.com/en-us/102654> (page publiée le 8 octobre 2025)
Aucune annonce de dépréciation trouvée — à considérer comme non vérifié plutôt que comme un non.

**Fragilité opérationnelle** : rate limiting **non documenté** (un développeur maintenant une synchro CardDAV iCloud depuis 8 ans rapporte des « rate limit exceeded » soudains, avec « *nothing in Apple's documentation relating to these limits* » — <https://developer.apple.com/forums/thread/722170>), vagues de 503 (<https://mjtsai.com/blog/2022/01/24/increased-icloud-errors/>), et Apple n'a **jamais officiellement supporté CalDAV**.

**L'ouverture : Apple Rappels comme CLIENT d'un serveur CalDAV tiers fonctionne toujours.** Réglages → Calendrier → Comptes → Autre → Ajouter un compte CalDAV expose un interrupteur **« Rappels »** en plus de « Calendriers ».

> **Preuve décisive et récente** : ticket Vikunja #2658, ouvert le **19 avril 2026** sur **iOS 26.4.1** — « *iOS Reminders correctly **pushes** changes to Vikunja over CalDAV, but doesn't fetch changes made on the Vikunja side* ». C'est une **régression de fetch** : la connexion existe et fonctionne en production (le push marche). Corrigé par la PR #2721.
> <https://github.com/go-vikunja/vikunja/issues/2658>

Corroboré par <https://tasks.org/docs/client_apple_reminders/> (« *Your Tasks.org lists will appear in Reminders* »), <https://github.com/nextcloud/tasks>, et [une procédure pas-à-pas](https://portal.thobson.com/knowledgebase/226/How-to-sync-calendars-and-tasks-to-an-iOS-device-using-CalDAV.html) mentionnant l'étape « *Choose Calendars and/or Reminders (tasks)* ».

Frictions connues : **sous-tâches aplaties** (la hiérarchie `RELATED-TO` s'affiche à plat), cas de listes invisibles sur iOS alors que macOS fonctionne (<https://github.com/sabre-io/Baikal/issues/995>, non résolu).

### 4.3 Google Tasks / Keep / Assistant

**Google Tasks API : oui, et c'est la seule voie Google viable.** `tasks v1`, `https://tasks.googleapis.com`. Création triviale : `POST /tasks/v1/lists/{tasklist}/tasks` avec `{"title": "Lait"}` (title ≤ 1024 caractères). **Pas de batch documenté** → 1 requête HTTP par article. Quota : **50 000 requêtes/jour**, aucune limite par minute publiée, aucune tarification (absence de page pricing, pas une déclaration explicite de gratuité).
Sources : <https://developers.google.com/workspace/tasks/overview> · <https://developers.google.com/workspace/tasks/reference/rest/v1/tasks/insert> · <https://developers.google.com/workspace/tasks/limits>

**Classification du scope — le point qui coûte.** ✅ `auth/tasks` **n'est PAS restricted** : la liste des scopes restreints est fermée et énumérée (Gmail, Drive, Fit, Chat, Data Portability, Photos Ambient, Health). ⚠️ **Il est donc *sensitive*** par application de la définition officielle (« *Sensitive scopes are scopes that request access to private user data* ») — mais **aucune page Google ne le nomme littéralement comme tel**. C'est une déduction rigoureuse, pas une citation. **Test décisif de 2 minutes : ajouter le scope dans Google Cloud Console → Google Auth Platform → Data Access et regarder sous quelle section il tombe.** À faire avant tout engagement.

Conséquences si sensitive (scénario probable) : domaine vérifié en Search Console, homepage, politique de confidentialité sur le même domaine, vidéo YouTube du flux OAuth, jusqu'à **10 jours** de review (page MàJ 17 juillet 2026) — **mais pas de CASA et pas de re-certification annuelle**, ce qui change radicalement le calcul par rapport à Gmail (§2.1).
Sources : <https://support.google.com/cloud/answer/13464325> · <https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification> · <https://support.google.com/cloud/answer/13465431>

Exemption sous 100 utilisateurs toujours valable (« *Personal Use apps: if the app is for your personal use (fewer than 100 users)… users will be allowed to click through "unverified app" warning screens* »), **plafond cumulé sur la vie du projet, non réinitialisable**.
Et **le piège « Testing » s'applique ici aussi** : « *Authorizations by a test user will expire seven days from the time of consent… that token will also expire* » → **passer en « In production » dès le départ**.
Sources : <https://support.google.com/cloud/answer/13464323> · <https://support.google.com/cloud/answer/15549945>

**Point de veille** : depuis le 1er mai 2026 Google resserre les quotas Workspace (Gmail/Calendar/Drive d'abord), et « *Later in 2026 […] API usage over standard daily thresholds will generate charges on your Google Cloud bill* » (<https://developers.google.com/workspace/tools-safety>).

**Google Keep API : modèle de données parfait, chemin d'autorisation inaccessible.** L'API existe (`keep v1`, `notes.create`, ressource `Note` avec `body.list` / `ListItem{text, checked}` jusqu'à 1000 items) — c'est **exactement** une liste de courses. Mais : « *The Google Keep API is used **in an enterprise environment*** », « *allowing **enterprise administrators** to manage Google Keep notes* », et **les deux seuls modes d'autorisation documentés sont des variantes de la délégation à l'échelle du domaine**, le quickstart exigeant « *domain-wide delegation of authority in the Google Workspace Admin console by a **super administrator account*** ». Un compte @gmail.com n'a ni domaine, ni console d'admin, ni super-admin.
Sources : <https://developers.google.com/workspace/keep/api/reference/rest> · <https://developers.google.com/workspace/keep/api/guides>
⚠️ **Non vérifié** : **aucune phrase Google n'interdit explicitement les comptes personnels**, ni ne liste les éditions Workspace requises (cherché dans la référence REST, les guides, le quickstart, la page produit, le discovery doc, les archives Workspace Updates). La restriction est massivement indiquée par tout le chemin d'autorisation, **jamais énoncée noir sur blanc**. → **Écarter : concevoir dessus, c'est parier sur un comportement non documenté.**

**Shopping list / Assistant lists : aucune API, et il n'y en a jamais eu.** Preuve machine-readable : l'annuaire officiel de découverte de toutes les APIs Google publiques (**523 APIs** au 3 août 2026) ne contient comme entrées approchantes que `keep v1`, `tasks v1`, `content v2.1` (Merchant Center, sans rapport) et `homegraph v1`. **Aucune API de listes.**
Source : <https://www.googleapis.com/discovery/v1/apis>
Historique : les listes Assistant ont quitté Keep pour Google Home/Express en avril 2017, puis y sont revenues (données non migrées supprimées après le 1er mai 2024) ; les **Conversational Actions — seule surface dev Assistant tierce — sont « *deprecated on June 13, 2023* »** et n'ont de toute façon jamais exposé les listes de l'utilisateur ; les Google Home APIs actuelles ont un périmètre Matter/Thread/appareils (0 occurrence de « shopping list » ou « grocery » sur leurs index).
Sources : <https://support.google.com/assistant/answer/14171370> · <https://developers.google.com/assistant/ca-sunset> · <https://developers.home.google.com/>

**Google Calendar + CalDAV VTODO : refus explicite.**
> « *Data exposed in the CalDAV interface is formatted according to the iCalendar specification. **Doesn't support `VTODO` or `VJOURNAL` data.*** »
> <https://developers.google.com/workspace/calendar/caldav/v2/guide>

### 4.4 Raccourcis Apple — entièrement faisable, deux points à tester

**`Get Contents of URL`** supporte GET, POST, PUT, PATCH, DELETE, et « *Request Body allows you to send **JSON**, a Form, or a File* ».
Source : <https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios>
⚠️ **Les en-têtes HTTP ne sont PAS documentés par Apple** — vérifié sur cette page (iOS et Mac) et sur toute la section « Use Web APIs in Shortcuts » : zéro occurrence de « Headers », aucune page sur l'authentification. Le paramètre **existe** (action `is.workflow.actions.downloadurl`, paramètres URL / Method / **Headers** / Request Body) mais n'est sourçable que via une base tierce sérieuse (<https://matthewcassinelli.com/actions/get-contents-of-url/>, ex-équipe Workflow/Shortcuts). **Un `Authorization: Bearer …` est faisable, à valider sur appareil.**

Limite officielle notable : « *OAuth 2 […] is currently **not supported*** » (<https://support.apple.com/guide/shortcuts/api-limitations-apd891a6c84e/9.0/ios/26>) → prévoir un **token statique**, pas un flux OAuth.

**Chaîne complète, entièrement documentée par Apple** :
`Get Contents of URL (POST/JSON)` → `Get Dictionary Value` (« *The data dictionary is actually a **list of dictionaries*** ») → `Repeat with Each` (« *runs the same group of actions **one time for each item*** », variable `Repeat Item`) → **`Add New Reminder`** (« *Creates a new reminder and adds it to the **selected list of reminders*** », paramètres Reminder / **List** / Alert / Priority / Flag / URL / Notes).
→ **Oui, on peut cibler une liste « Courses » précise.**
Sources : <https://support.apple.com/guide/shortcuts/get-dictionary-value-action-apdf01294032/ios> · <https://support.apple.com/guide/shortcuts/use-repeat-actions-apdc11deb2c1/ios> · <https://matthewcassinelli.com/actions/add-new-reminder/>
⚠️ **Non vérifié** : le nom exact de l'action livrée en iOS 26 stable — Apple teste depuis iOS 18 un remplacement App Intents nommé `Create Reminder`, et ne publie aucun référentiel officiel d'actions.

**Distribution : le réglage « Raccourcis non fiables » n'existe plus.** Preuve par diff de **la même page du guide Apple à deux versions** :

| Version | Titre | Contenu |
|---|---|---|
| Guide 3.2 (ère iOS 14) | « Enable shared shortcuts » | Activer Réglages → Raccourcis → **Allow Untrusted Shortcuts** |
| Guide 9.0 / iOS 26 | « Advanced Privacy and security settings » | **Aucune** mention ; ne reste que « Allow Running Scripts » et l'analyse anti-malware |

<https://support.apple.com/en-kz/guide/shortcuts/enable-shared-shortcuts-apdfeb05586f/3.2/ios> vs <https://support.apple.com/guide/shortcuts/apdfeb05586f/9.0/ios/26>
(Le retrait est daté d'iOS 15 par les fils communautaires — date non confirmée officiellement, mais **l'absence actuelle du réglage est établie par la doc Apple**.)

**Parcours réel en 2026** : lien iCloud → écran de présentation → **« Get Shortcut »**, sans avertissement. Le partage « Anyone » implique que « *Apple will receive a copy of your shortcut for validation* » ; révocable via « Stop Sharing ».
**Et la brique clé : les Import Questions** — « *When the recipient runs the shortcut, they're presented with the import questions […] the shortcut is populated with the user's own information* », et le champ « *is cleared when the shortcut is shared* ». **C'est le mécanisme prévu pour faire saisir à chaque utilisateur son URL d'API et son token, sans coder de secret dans le raccourci partagé.**
Sources : <https://support.apple.com/guide/shortcuts/share-shortcuts-apdf01f8c054/ios> · <https://support.apple.com/guide/shortcuts/add-import-questions-to-shared-shortcuts-apdf330fd3a0/9.0/ios/26>

**Automatisations horaires sans interaction : oui.** « *Some personal automations can run without asking you for confirmation* » (désactiver « Ask Before Running », puis « Don't Ask ») ; déclencheur **Time of Day** documenté. Contrepartie : en mode « Run Immediately », **la notification devient obligatoire** (le toggle « Notify When Run » disparaît). Et **les automatisations sont locales à l'appareil, elles ne se synchronisent pas.**
Sources : <https://support.apple.com/guide/shortcuts/enable-or-disable-a-personal-automation-apd602971e63/ios> · <https://support.apple.com/guide/shortcuts/event-triggers-apd932ff833f/ios>

⚠️ **Deux angles morts à tester sur appareil (une demi-journée)** : (a) le paramètre `Headers`, non documenté par Apple ; (b) le prompt de confidentialité au premier accès réseau — Apple documente le dialogue générique (Allow Once / Always Allow / Don't Allow) mais **aucune page ne décrit un prompt par domaine web**, et des fils communautaires décrivent des demandes répétées. **C'est le principal risque UX de ce scénario.**

**Android : l'écart est structurel, et il n'est pas où on l'attend.** L'appel HTTP est un problème résolu et gratuit — **HTTP Shortcuts** (`ch.rmy.android.http_shortcuts`, MIT, v4.6.0 du 18 juin 2026 sur F-Droid) fait toutes les méthodes, auth Basic/Digest/**Bearer**/certificat client, en-têtes et corps personnalisés, JavaScript avant/après ; **Tasker** (~4,49 USD) aussi, l'exemple de sa doc étant littéralement `Authorization:Bearer MY_ACCESS_TOKEN`.
**Le maillon manquant est l'écriture dans une liste grand public** : il n'existe **aucun contract Android standard** pour les tâches (le framework a `CalendarContract`, rien pour les listes) ; `actions.intent.UPDATE_ITEM_LIST` est un intent qu'une app **déclare pour recevoir** des commandes Assistant, pas un canal d'injection, il est **en dépréciation** et en-US uniquement ; pour Keep, le seul mécanisme constaté est `Intent.ACTION_SEND` vers `com.google.android.keep` — **non documenté**, crée une nouvelle note, ne cible pas une liste. Et le plugin Tasker officiel de Tasks.org « *can only set the title, due date, due time, priority, and description* » — **le choix de la liste n'est pas exposé**.
Sources : <https://f-droid.org/en/packages/ch.rmy.android.http_shortcuts/> · <https://tasker.joaoapps.com/userguide/en/help/ah_http_request.html> · <https://developer.android.com/reference/app-actions/built-in-intents/productivity/update-item-list> · <https://tasks.org/docs/tasker/>

| | iOS / Raccourcis | Android |
|---|---|---|
| App d'automatisation | **Préinstallée** | À installer |
| Installation en 1 lien | **Oui** (lien iCloud) | **Non** — pas d'équivalent |
| Saisie URL/token par l'utilisateur | **Import questions**, prévu pour | Variables à créer à la main |
| Écriture dans l'app de tâches native | **Oui, liste au choix** | **Non** |
| Planification | Intégrée | Tasker/MacroDroid en plus |

→ **Sur Android, la vraie solution n'est pas sur le téléphone, elle est côté serveur.** Pour un public non technique, « rien à installer » bat structurellement « installer Tasker et configurer une macro ».

### 4.5 Standards ouverts

**RFC 4791** = le protocole CalDAV ; **RFC 5545** = le format iCalendar dont `VTODO` (§3.6.2). Contrainte à connaître : RFC 4791 §4.1 **interdit** de mélanger VEVENT et VTODO dans une même ressource-objet. Évolution en cours : `draft-ietf-calext-ical-tasks-17` (10 décembre 2025), soumis à l'IESG.
Sources : <https://www.ietf.org/rfc/rfc4791.txt> · <https://www.rfc-editor.org/rfc/rfc5545.html> · <https://datatracker.ietf.org/doc/draft-ietf-calext-ical-tasks/>

**Qui consomme réellement des VTODO** : Apple Rappels (comme client d'un serveur tiers — §4.2), Thunderbird (« *implements `VEVENT` events and `VTODO` tasks* »), DAVx⁵ qui route les VTODO vers **jtx Board, OpenTasks et Tasks.org**, Nextcloud Tasks, Nextcloud Deck, Vikunja, Evolution. **Google Calendar : non, refus explicite.**
⚠️ **Modèle économique à connaître** : chez Tasks.org, Google Tasks et Microsoft To Do sont sans abonnement, mais **CalDAV nécessite un abonnement in-app** (ou un parrainage GitHub) — <https://tasks.org/docs/sync/>. C'est une friction réelle sur Android.

**Flux .ics / webcal:// contenant des VTODO — l'intuition est probablement juste, sans preuve formelle.**
- **Google Calendar ignore les VTODO** : « *When you import from an ICS file into Google Calendar, it only imports calendar entries from that file; **it ignores tasks ("VTODO" entries)*** » (<https://groups.google.com/g/tasks-backup/c/YVUSYThNtl8>, modérateur du projet). ⚠️ La citation porte sur l'**import de fichier** ; aucune source Google explicite sur « Ajouter par URL ». Cohérent, non prouvé.
- **iOS : NON VÉRIFIABLE FORMELLEMENT.** Deux éléments seulement : un rapport utilisateur direct **resté sans réponse valable** (1er octobre 2023, « *I have created a subscribed calendar with some reminders (VTODO) being generated, but these reminders are not appearing in the Reminders app* » — <https://discussions.apple.com/thread/255169909>) ; et un **signal industriel fort** — Todoist, qui a exactement ce besoin, **n'émet pas de VTODO** dans son flux iCal, il convertit les tâches en événements (« *Tasks with a date but without a time will appear as all-day events* »). S'il existait un chemin abonnement → Rappels, Todoist l'utiliserait.
- Contre-exemple utile : **Tasks.org ne sait pas** s'abonner à un flux ICS de VTODO — ticket ouvert **depuis le 28 janvier 2015**, aucune PR (<https://github.com/tasks/tasks/issues/235>).
→ **Ne pas investir dans cette voie.** Si l'on veut un flux, émettre des **VEVENT** (approche Todoist) — mais ça atterrit dans l'agenda, pas dans une liste cochable.

**Web Share API — le meilleur rapport couverture/effort.** Support **90,3 % global** au 3 août 2026 : Safari iOS ✅ (12.2+), Chrome Android ✅, Chrome desktop ✅ (128+), Edge ✅ (95+), **Firefox desktop ❌**.
Source : <https://caniuse.com/web-share>
Contraintes : HTTPS obligatoire ; **activation transitoire requise** (« *must be triggered off a UI event like a button click* », sinon `NotAllowedError`) ; iframes tiers nécessitent `allow="web-share"`.
Sur iOS, **Rappels est bien une cible de la feuille de partage native**.

⚠️ **Deux bugs iOS documentés et toujours signalés en mars 2024** (<https://developer.apple.com/forums/thread/724641>) : (1) la **query string est supprimée** au partage via Messages/Messenger ; (2) une **URL cross-domain est remplacée par l'URL de la page courante**. Contournement rapporté : mettre l'URL dans `text`.
→ **Conséquence directe : sur iOS, `url` est traité comme « l'URL de la page qu'on partage », pas comme une donnée arbitraire. Pour une liste de courses, tout mettre dans `text` et ne pas fournir `url` du tout.**

⚠️ Un article du 7 janvier 2026 note que pour du texte sélectionné, « *Longer selections often **generate multiple suggested reminders at once*** » — **mais cela relève d'Apple Intelligence**, donc conditionné au matériel et aux réglages langue/région. Bonus non garanti, à ne pas promettre.
Source : <https://appleinsider.com/articles/26/01/08/how-to-turn-emails-webpages-notes-into-reminders-with-apple-intelligence>

**Web Share *Target*** (PWA qui *reçoit* un partage) : Chrome desktop 89, Chrome Android 76, Edge, Samsung — **Firefox `false`, Safari `false`, Safari iOS `false`**. **Android/Chromium uniquement, aucun chemin iOS.**

**Copier-coller multi-lignes : non, et c'est une régression.** Sur Apple Rappels, coller un bloc multi-lignes crée **UN SEUL rappel** contenant toute la liste — « *pasting a list into Reminders **stopped** creating a list of reminders items* » (23 janvier 2021), confirmé sur macOS Sonoma 14.1 en novembre 2023.
Sources : <https://nowicki.dev/how-to-import-a-list-into-apple-reminders/> · <https://discussions.apple.com/thread/255303302> · <https://talk.tidbits.com/t/importing-a-list-into-reminders/21034>
Contournement fiable, le « truc Notes » : coller dans **Notes** → convertir en checklist → copier → coller dans **Rappels** → un rappel par ligne. ⚠️ Non documenté par Apple, instable dans le temps.
⚠️ **Google Keep : NON VÉRIFIÉ** — aucune source, ni première ni seconde main, ne confirme qu'un collage multi-lignes crée une case par ligne. Google ne documente que la conversion manuelle. **À tester avant d'en faire une hypothèse d'architecture.**

### 4.6 Alternatives tierces, sous-estimées

- **Todoist REST v1** : « *You can use our API for free* », OAuth2 **ou token personnel**, création de tâche dans un projet, endpoint `/sync` pour le batch. Effort quasi nul. <https://developer.todoist.com/api/v1/>
- **Microsoft To Do via Graph** : `POST /me/todo/lists/{id}/tasks`, avec `checklistItem` pour les sous-éléments, delta query, permissions déléguées. Fonctionne sur comptes personnels **et** pro. <https://learn.microsoft.com/en-us/graph/api/resources/todo-overview>
- **Bring!** (l'app de courses dominante en Suisse) : **aucune API officielle**. Les intégrations (node-bring-api, Home Assistant) reposent sur une API non documentée et reverse-engineered, avec disclaimer explicite « *in no way endorsed by or affiliated with Bring! Labs AG* ». Techniquement ça marche et c'est très utilisé, **mais c'est un pari sur un endpoint privé** — exactement le motif pour lequel l'ADR écarte les drives d'enseignes. **Cohérence oblige : non.**
  <https://github.com/foxriver76/node-bring-api>

### 4.7 Classement effort / valeur

| Rang | Voie | Effort | Couverture | Verdict |
|---|---|---|---|---|
| **1** | **`navigator.share({ text })`** + repli `clipboard.writeText()` | **~1 jour** | iOS ✅ Android ✅ desktop ✅ (sauf Firefox) | ✅ **À faire en premier, sans discussion.** ~90 % du bénéfice pour ~5 % de l'effort. Sans `url`. |
| **2** | **Endpoint CalDAV / VTODO** servi par notre backend | **Élevé** (serveur CalDAV, auth, ETags, sync-tokens) | iOS ✅ (compte natif) Android ✅ (DAVx⁵ + Tasks.org) desktop ✅ | ✅ **Le seul vrai standard ouvert qui aboutit dans les apps natives des deux plateformes, avec ZÉRO installation côté téléphone.** Le contrat est un RFC, pas une API propriétaire qui peut fermer. |
| **3** | **Raccourci iOS** par lien iCloud + import questions | ~2–3 j + doc utilisateur | iOS uniquement | ✅ **Excellent sur iPhone.** Zéro friction d'installation depuis iOS 15, un rappel par article dans la bonne liste, automatisation horaire possible. **2 tests à faire d'abord** (Headers, prompt réseau). |
| **4** | **Google Tasks API** | Moyen (OAuth + review ~10 j) | Android surtout | ⚠️ Seule voie Google. **Pas de CASA, pas de re-certif annuelle** — c'est le bon côté du sensitive. Bloquants : plafond **100 utilisateurs cumulés à vie** sans vérification, et Google Tasks n'est pas là où les gens font leurs courses. |
| **5** | **Todoist / Microsoft To Do** | Faible | Utilisateurs de ces apps | ⚠️ Effort quasi nul, public restreint. Bon candidat « bonus ». |
| ❌ | Flux .ics / webcal:// de VTODO | Faible | **Probablement nulle sur mobile** | Aucun client grand public mobile ne le consomme de façon prouvée. |
| ❌ | Automatisation Android (Tasker / HTTP Shortcuts) | Élevé **pour l'utilisateur** | Android technophile | L'appel HTTP est gratuit et mature, mais **rien ne permet d'écrire dans Keep ou Tasks depuis le téléphone**. |
| ❌ | Google Keep API | — | Workspace uniquement | Modèle de données idéal, chemin d'autorisation entreprise-only. |
| ❌ | Bring! | Moyen | Utilisateurs Bring! | API non officielle — incohérent avec l'ADR sur les drives. |
| ❌ | Écrire dans les Rappels iCloud depuis un serveur | — | — | **Impossible.** Ni API, ni CloudKit, ni CalDAV. Pas une question d'effort. |

---

## 5. Récapitulatif des coûts cachés

| Coût | Où il se cache | Montant / impact |
|---|---|---|
| **Audit CASA annuel** | Gmail API (`gmail.readonly` = restricted) | **675 $/an minimum, à perpétuité**, self-scan gratuit supprimé, audit complet à chaque renouvellement |
| **Plafond 100 utilisateurs à vie** | Toute app Google non vérifiée (Gmail **et** Tasks) | Non réinitialisable, cumulé sur la vie du projet |
| **Refresh tokens à 7 jours** | Publishing status « Testing » (Gmail et Tasks) | Reconnexion hebdomadaire — rédhibitoire |
| **Changement de mot de passe utilisateur** | Google (scopes Gmail) et Apple (app passwords) | Casse l'intégration **silencieusement** ; flux de tickets support garanti |
| **Rejet DMARC sur mails transférés** | Cloudflare Email Routing | Perte **silencieuse** de mails de commande |
| **Ticket d'entrée inbound Postmark** | Absent de Free et Basic | 16,50 $/mois, pas 0 $ |
| **Le modèle maquille l'arithmétique** | VLM sur tickets | Neutralise partiellement le contrôle `Σ lignes == total` |
| **Absence de logprobs chez Anthropic** | API Claude | Aucun signal de confiance natif : coût structurel à budgéter dès la conception |
| **10 recherches/min chez Open Food Facts** | API en ligne | ≈ 1 ticket/minute → dump local obligatoire |
| **Lexique d'abréviations d'enseignes FR** | N'existe nulle part | Coût de démarrage entièrement à notre charge (et notre seul actif défendable) |
| **AGPL** | Stalwart, ParadeDB `pg_search` | S'applique si Pantry devient un service tiers |
| **Abonnement Tasks.org** | Connecteur CalDAV sur Android | Friction utilisateur réelle |
| **Papier thermique** | Physique | Ticket illisible en **7 à 30 jours** ; le portefeuille PVC accélère la destruction |

## 6. Points explicitement non vérifiés

**Bloquants pour une décision** :
1. **Le port 25 entrant est-il bloqué chez notre hébergeur ?** Ni Hetzner ni DigitalOcean ne précisent la direction du blocage. → test `nc -l 25`, 5 minutes.
2. **Classification exacte du scope `auth/tasks`** — déduite rigoureusement, jamais citée par Google. → test en console Cloud, 2 minutes.
3. **Taux de complétion `product_name_fr` / `brands` / `quantity` sur le sous-ensemble France d'OFF** — publié nulle part, détermine la stratégie de matching. → 10 min de SQL sur le dump.
4. **Le paramètre `Headers` de « Get Contents of URL »** — absent de toute la doc Apple. → test sur appareil.
5. **Le prompt de confidentialité réseau dans Raccourcis** (par domaine ou non) — aucune page Apple. → test sur appareil.

**Non tranchés faute de source** :
6. Dispense de CASA pour les apps stockant les données uniquement côté client (question posée sur le forum officiel Google en mars 2026, **restée sans réponse**).
7. Classification de `gmail.addons.current.message.readonly` (deux pages officielles se contredisent).
8. Recevabilité d'une app de garde-manger au titre du type autorisé n°4 de Google.
9. Tarification 2026 de SendGrid (pages en boucle de redirection) et de Brevo (montants en JS).
10. Plan Mailjet ouvrant la Parse API (« Crystal » n'existe plus) et localisation de ses données.
11. Taille max de message inbound chez Postmark, Brevo, ImprovMX, Resend, Mailtrap.
12. MX de la région EU chez Mailgun ; où sont stockés les mails **reçus** chez Resend.
13. Cloudflare Email Routing exige-t-il un domaine en full setup ?
14. Périmètre exact du sous-traitant OpenAI chez CloudMailin.
15. Blocage de l'auto-forwarding externe chez Microsoft 365 (source = blog, pas learn.microsoft.com).
16. Comportement d'un abonnement webcal:// contenant des VTODO sur iOS 26/27.
17. Interdiction explicite des comptes @gmail.com sur l'API Keep (introuvable, mais massivement indiquée).
18. Collage multi-lignes dans une checklist Google Keep — zéro source.
19. Nom exact de l'action Rappels en iOS 26 (`Add New Reminder` vs `Create Reminder`).
20. Dépréciation des mots de passe d'app Apple — aucune annonce trouvée.

**Trous du champ, pas de cette recherche** :
21. Aucune étude ne quantifie la chute du taux OCR selon l'âge d'un ticket thermique.
22. Aucune publication sur le *receipt image stitching* ni sur le tuilage avec chevauchement pour VLM.
23. Aucune comparaison publiée « VLM avec vs sans dewarping sur tickets froissés ».
24. Aucun benchmark d'embedding n'évalue du libellé abrégé.
25. Aucun dataset ni lexique public de tickets/abréviations français.
26. Aucun benchmark fiable de `pg_trgm` sur 50 k – 2 M lignes.
27. Spécificités métier FR des tickets (poids variable, lignes négatives, TVA multi-taux, consignes) — non couvertes.

**Réserves sur des chiffres cités** : page de tarifs Google Document AI jamais chargée intégralement ; page Azure avec placeholders `$-` ; tarification Mindee ambiguë (×12 d'écart) ; chiffres de dégradation par angle issus de snippets ; +34 points de LightOnOCR-2 sur Old Scans annoncés par les auteurs eux-mêmes ; `k=60` du RRF confirmé par l'implémentation pgvector et non par le papier source ; la sous-section « boucle d'apprentissage » (§3.6) n'est pas sourcée.

---

## 7. Décisions recommandées

1. **Ingestion par email entrant, pas par lecture de boîte.** C'est la voie principale. Elle supprime intégralement la vérification OAuth, l'audit CASA (675 $/an à perpétuité), le plafond des 100 utilisateurs et le stockage de secrets d'utilisateurs.

2. **Auto-héberger la réception avec Stalwart + MTA Hook**, sous réserve de trois vérifications préalables : port 25 entrant ouvert, doc MTA Hooks lue dans un navigateur, AGPL arbitrée. **Repli MIT immédiat : Postal.** **Repli managé : CloudMailin** (seul à forcer la région UE par DNS) ou **ImprovMX Premium** à 9 $/mois (datacenters FR chez OVH).

3. **Écarter Cloudflare Email Routing** malgré sa gratuité : il rejette sur DMARC, donc il fera disparaître silencieusement une partie des mails transférés depuis Gmail. Écarter aussi SendGrid (tarifs invérifiables, aucune sécurité de webhook), Resend (webhook sans le corps du mail, données de compte aux États-Unis) et Postmark (16,50 $/mois, aucune résidence UE).

4. **Traiter la capture du mail de confirmation Gmail comme une story à part entière.** Sans elle, l'onboarding est bloqué. Et recommander à l'utilisateur un **filtre de transfert sélectif** (expéditeur = enseigne) : c'est de la minimisation RGPD, pas du confort.

5. **Ne pas implémenter la Gmail API.** Si l'automatisation par lecture de boîte redevient un sujet, commencer par **Microsoft** (OAuth délégué sur `IMAP.AccessAsUser.All`, sans CASA, sans audit payant, publisher verification gratuite) — c'est le seul grand fournisseur où un dev solo peut faire les choses proprement.

6. **Tickets : pipeline hybride VLM + OCR classique, avec revue humaine obligatoire au départ.** Le VLM seul est disqualifié par le maquillage arithmétique documenté par ReceiptBench — il fabrique des lignes pour faire tomber le total. L'OCR classique en seconde jambe coûte quasi rien en CPU et fournit **le seul signal de confiance disponible** : Anthropic n'expose pas de logprobs, et surtout le port `ModelProvider` de l'ADR-0005 ne peut dépendre d'aucune fonctionnalité propre à un adaptateur. **Le signal de confiance doit vivre au-dessus du port.** Proposer **Claude Sonnet 5** comme défaut d'interface (≈ 2,5 ¢/ticket, à la charge du foyer), sans jamais conditionner la revue humaine au fournisseur choisi.

7. **Valider par schéma JSON contraint + Pydantic, et valider contre l'image, pas contre la cohérence interne.** Les contraintes numériques (`minimum`, `maximum`) ne sont pas supportées par le schéma — seul `enum` l'est, utilisable pour les taux de TVA. Le contrôle `Σ lignes == total` reste un détecteur d'échec franc, jamais un certificat de justesse.

8. **Amender l'ADR-0008 : le dump Open Food Facts local devient un prérequis de phase 1, pas de phase 2.** L'ADR raisonnait sur le scan EAN (~15 req/min, une requête par scan) ; le rapprochement de libellés consomme l'endpoint de **recherche** (**10 req/min**) à raison d'**une requête par ligne de ticket**. Un seul ticket sature la minute. Dump filtré France (~1,25 M lignes, format **Parquet**, pas CSV) **+ Ciqual pour le frais et le vrac** (les lignes au poids, à préfixe `02`/`20`–`29`, n'auront jamais de fiche OFF). Table d'alias dans une table séparée pour préserver la frontière ODbL.

9. **Le matching se joue sur l'expansion d'abréviations, pas sur le moteur de similarité.** « PDT NOUV 1KG » et « Pommes de terre nouvelles » ne partagent aucun trigramme. Construire le lexique par enseigne, l'auto-alimenter depuis les validations utilisateur, **et consigner aussi les rejets**. Utiliser `word_similarity` (pas `similarity`), et faire du lookup d'alias un court-circuit O(1) en tête de pipeline.

10. **Mesurer trois chiffres avant d'écrire du code** : la complétion des champs OFF France (10 min de SQL), la classification du scope `auth/tasks` (2 min en console), l'ouverture du port 25 entrant (5 min). Chacun peut invalider une branche entière de cette note.

11. **Export de liste : `navigator.share({ text })` en v1, sans `url`** (deux bugs iOS documentés). ~1 jour de travail, ~90 % du bénéfice, couverture iOS + Android + desktop, aucune dépendance à un programme de vérification.

12. **Servir un endpoint CalDAV/VTODO en v2.** C'est le seul chemin qui atterrit dans l'app **Rappels native iOS** — prouvé fonctionnel en avril 2026 sur iOS 26.4.1 — et qui couvre Android via DAVx⁵/Tasks.org, **sans rien installer sur le téléphone**. Le contrat est un RFC, pas une API propriétaire qui peut fermer.

13. **Écarter formellement** : écrire dans les Rappels iCloud depuis un serveur (impossible : ni API, ni CloudKit, ni CalDAV depuis iOS 13), l'API Google Keep (délégation domaine + super-admin Workspace), les flux .ics de VTODO (aucun client mobile grand public prouvé), l'automatisation Android côté téléphone (rien ne permet d'écrire dans Keep ou Tasks), et Bring! (API non officielle — même motif que l'ADR sur les drives d'enseignes).

14. **Raccourci iOS en v3, si la base installée est majoritairement iPhone.** Toute la chaîne est documentée et les *import questions* résolvent proprement la distribution du token. Deux tests sur appareil à faire d'abord (paramètre `Headers`, prompt de confidentialité réseau) : une demi-journée.

15. **Acter ces choix dans trois ADR** une fois les cinq vérifications de la §6 faites : *réception d'e-mail entrant auto-hébergée* (Stalwart vs Postal vs managé, avec l'arbitrage AGPL), *export de la liste de courses* (Web Share puis CalDAV), et un **amendement à l'ADR-0008** sur l'avancement du dump local en phase 1. La décision n°5 (ne pas implémenter la Gmail API) mérite d'être consignée elle aussi : c'est une non-décision coûteuse à réexaminer tous les six mois si elle n'est pas écrite.

16. **Rediscuter tout choix de fournisseur américain si le pourvoi Latombe aboutit.** L'arrêt *Trump v. Slaughter* du 29 juin 2026 a fragilisé l'indépendance de la FTC, l'un des piliers de l'adéquation DPF. Les recommandations n°2 (auto-hébergement) et n°8 (dump local) mettent Pantry à l'abri de ce risque par construction — c'est un argument de plus en leur faveur.
