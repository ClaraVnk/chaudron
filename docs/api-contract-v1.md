# Contrat d'API — v1 (tranche verticale)

Ce document fige l'interface entre le backend et la PWA pour la première tranche
fonctionnelle. Il est écrit **avant** le code des deux côtés, pour qu'ils
puissent être construits en parallèle sans diverger.

Portée volontairement réduite : inventaire, emplacements, résolution de
code-barres, suggestions de recettes. Le parsing de ticket et la réception
d'emails ne sont pas dans cette tranche.

Base : `/v1`. Tout est JSON. Les erreurs suivent RFC 9457 (`application/problem+json`).

---

## Authentification (tranche 1)

Pas de comptes utilisateurs dans cette tranche. Le foyer courant est résolu par
un en-tête :

```
X-Household-Id: <uuid>
```

Absent ou inconnu → `401`. Un foyer de démonstration est créé par le seed.

> Ce mécanisme est **provisoire et documenté comme tel**. Il existe pour que la
> tranche soit testable de bout en bout sans construire d'abord
> l'authentification. La forme du contrat ne change pas quand une vraie session
> arrivera : le foyer restera résolu côté serveur, jamais envoyé par le client.

---

## Santé

| Méthode | Chemin | Réponse |
|---|---|---|
| `GET` | `/healthz` | `200 {"status":"ok"}` — le processus vit. Ne touche pas la base. |
| `GET` | `/readyz` | `200 {"status":"ready","checks":{"database":"ok"}}` ou `503` avec le détail |

---

## Emplacements de stockage

`GET /v1/locations` →

```json
[{"id":"uuid","name":"Frigo","kind":"fridge","item_count":12}]
```

`kind` ∈ `fridge` | `freezer` | `pantry` | `cellar` | `other`.

---

## Inventaire

`GET /v1/inventory` — paramètres : `location_id`, `q`, `expiring_within_days`,
`limit` (défaut 50), `offset`.

```json
{
  "total": 37,
  "items": [{
    "id": "uuid",
    "product": {"id":"uuid","name":"Lait demi-écrémé","brand":"Lactel","gtin":"3033490004743","image_url":null},
    "location": {"id":"uuid","name":"Frigo","kind":"fridge"},
    "quantity": {"amount":"1.000","unit":"L"},
    "expires_on": "2026-08-12",
    "expiry_kind": "use_by",
    "opened_at": null,
    "source": "barcode_scan",
    "created_at": "2026-08-03T18:20:00Z"
  }]
}
```

`quantity.amount` est une **chaîne**, pas un nombre : les flottants JSON
détruisent les décimales exactes, et une quantité fausse d'un facteur dix dans un
inventaire alimentaire n'est pas un détail.

`source` ∈ `manual` | `barcode_scan` | `receipt_import`.

`POST /v1/inventory` →

```json
{"product_id":"uuid","location_id":"uuid","amount":"1.5","unit":"kg",
 "expires_on":"2026-08-20","expiry_kind":"best_before","source":"manual"}
```

`201` avec l'article créé. `product_id` **ou** `product` (objet de création de
produit manuel) doit être fourni, pas les deux.

`PATCH /v1/inventory/{id}` — champs modifiables : `amount`, `unit`,
`location_id`, `expires_on`, `expiry_kind`, `opened_at`.

`DELETE /v1/inventory/{id}` — `204`. Paramètre `reason` ∈ `consumed` |
`wasted` | `correction` (défaut `consumed`), journalisé dans `stock_movement`.

---

## Produits et code-barres

`GET /v1/products/lookup?gtin=3033490004743` →

- `200` avec la fiche produit (depuis le cache, sinon Open Food Facts, puis mise en cache)
- `404` `{"type":"...product-not-found","gtin":"..."}` si introuvable — le client
  bascule alors sur la saisie manuelle
- `422` si le code est un **code interne magasin** (préfixe `02`, `20`–`29`) :
  poids variable, il ne sera jamais dans un référentiel public. Le client doit
  détecter ce cas lui-même et ne pas appeler l'API du tout ; la vérification
  côté serveur est un filet.
- `503` si Open Food Facts est injoignable ou nous a limités, avec `Retry-After`

`POST /v1/products` — création manuelle : `{"name":"...","brand":null,"gtin":null,
"default_unit":"g"}` → `201`. Le produit est privé au foyer.

---

## Recettes

`POST /v1/recipes/suggest` →

```json
{"location_ids": [], "max_suggestions": 3, "notes": "rapide, sans four"}
```

Réponse `200` :

```json
{
  "provider_mode": "instance_owner",
  "model": "claude-opus-5",
  "suggestions": [{
    "id":"uuid","title":"Gratin de courgettes","summary":"...",
    "duration_minutes":35,"servings":4,
    "ingredients":[{"name":"Courgettes","amount":"600","unit":"g","in_stock":true}],
    "steps":["...","..."],
    "uses_expiring_soon": true
  }]
}
```

`409` `{"type":"...provider-not-configured"}` si le foyer n'a pas de fournisseur
utilisable. Le client affiche l'écran de configuration, pas une erreur brute.

---

## Capacités du fournisseur

`GET /v1/providers/capabilities` →

```json
{
  "configured": true,
  "mode": "instance_owner",
  "provider": "anthropic",
  "model": "claude-opus-5",
  "capabilities": {"vision": true, "structured_output": true},
  "degraded": false,
  "degraded_reasons": []
}
```

Quand `degraded` vaut `true`, `degraded_reasons` liste en clair ce qui est
réduit ou indisponible et pourquoi. **La PWA affiche cet état en permanence, pas
au moment de l'échec** : l'utilisateur doit connaître la limite avant d'essayer,
pas après.

---

## Forme des erreurs

```json
{"type":"https://chaudron.dev/problems/product-not-found",
 "title":"Product not found","status":404,
 "detail":"No product matches GTIN 3033490004743.","gtin":"3033490004743"}
```

Aucune erreur ne renvoie de trace d'exception, et aucune ne fait fuiter une clé
de fournisseur — y compris dans `detail`.
