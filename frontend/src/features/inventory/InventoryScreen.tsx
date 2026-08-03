import { useCallback, useEffect, useMemo, useState } from 'react';
import { describeError } from '../../api/client';
import { deleteInventoryItem, getInventory } from '../../api/endpoints';
import type { InventoryItem, RemovalReason, StorageLocation } from '../../api/types';
import { Button, Chip, ChipRow, EmptyState, ErrorState, LoadingRow } from '../../components/ui';
import { SOON_DAYS, locationKindLabel } from '../../lib/expiry';
import { useDebouncedValue } from '../../hooks/useDebouncedValue';
import { InventoryItemRow } from './InventoryItemRow';
import styles from './Inventory.module.css';

const PAGE_SIZE = 50;

/** Stable identity so memo dependencies do not change on every render. */
const NO_ITEMS: InventoryItem[] = [];

interface Props {
  locations: StorageLocation[];
  locationsError: string | null;
  /** Bumped by the shell whenever an item is added elsewhere. */
  refreshToken: number;
  onChanged: () => void;
  onAddItem: () => void;
}

interface Group {
  id: string;
  name: string;
  kindLabel: string;
  items: InventoryItem[];
}

/** The answer currently held, tagged with the query it answers. */
interface Result {
  key: string;
  items: InventoryItem[];
  total: number;
  error: string | null;
}

function groupByLocation(items: InventoryItem[], locations: StorageLocation[]): Group[] {
  const order = new Map(locations.map((location, index) => [location.id, index]));
  const groups = new Map<string, Group>();

  for (const item of items) {
    const existing = groups.get(item.location.id);
    if (existing) {
      existing.items.push(item);
      continue;
    }
    groups.set(item.location.id, {
      id: item.location.id,
      name: item.location.name,
      kindLabel: locationKindLabel(item.location.kind),
      items: [item],
    });
  }

  const rank = (id: string) => order.get(id) ?? Number.MAX_SAFE_INTEGER;
  return [...groups.values()].sort((a, b) => rank(a.id) - rank(b.id));
}

export function InventoryScreen({
  locations,
  locationsError,
  refreshToken,
  onChanged,
  onAddItem,
}: Props) {
  const [search, setSearch] = useState('');
  const [locationId, setLocationId] = useState<string | null>(null);
  const [expiringOnly, setExpiringOnly] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [result, setResult] = useState<Result | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const debouncedSearch = useDebouncedValue(search, 300);
  const trimmedSearch = debouncedSearch.trim();
  const filtered = trimmedSearch !== '' || locationId !== null || expiringOnly;

  const query = useMemo(
    () => ({
      q: trimmedSearch === '' ? undefined : trimmedSearch,
      location_id: locationId ?? undefined,
      expiring_within_days: expiringOnly ? SOON_DAYS : undefined,
      limit: PAGE_SIZE,
    }),
    [trimmedSearch, locationId, expiringOnly],
  );

  // Identifies the request; `loading` is this key not matching the held answer.
  // Deriving it avoids a synchronous setState inside the effect and keeps the
  // previous page visible while the next one is in flight.
  const queryKey = `${JSON.stringify(query)}|${String(refreshToken)}|${String(reloadNonce)}`;

  useEffect(() => {
    const controller = new AbortController();

    getInventory({ ...query, offset: 0 }, controller.signal)
      .then((page) => {
        setResult({ key: queryKey, items: page.items, total: page.total, error: null });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ key: queryKey, items: [], total: 0, error: describeError(cause) });
      });

    return () => {
      controller.abort();
    };
  }, [query, queryKey]);

  const loading = result?.key !== queryKey;
  const items = result?.items ?? NO_ITEMS;
  const total = result?.total ?? 0;
  const error = result?.error ?? null;

  const loadMore = useCallback(() => {
    setLoadingMore(true);
    getInventory({ ...query, offset: items.length })
      .then((page) => {
        setResult((previous) =>
          previous === null
            ? previous
            : { ...previous, items: [...previous.items, ...page.items], total: page.total },
        );
      })
      .catch((cause: unknown) => {
        setResult((previous) =>
          previous === null ? previous : { ...previous, error: describeError(cause) },
        );
      })
      .finally(() => {
        setLoadingMore(false);
      });
  }, [query, items.length]);

  const removeItem = useCallback(
    async (id: string, reason: RemovalReason) => {
      try {
        await deleteInventoryItem(id, reason);
        setResult((previous) =>
          previous === null
            ? previous
            : {
                ...previous,
                items: previous.items.filter((item) => item.id !== id),
                total: Math.max(0, previous.total - 1),
              },
        );
        onChanged();
      } catch (cause: unknown) {
        setResult((previous) =>
          previous === null ? previous : { ...previous, error: describeError(cause) },
        );
      }
    },
    [onChanged],
  );

  const groups = useMemo(() => groupByLocation(items, locations), [items, locations]);

  const resetFilters = () => {
    setSearch('');
    setLocationId(null);
    setExpiringOnly(false);
  };

  return (
    <section className={styles.screen} aria-labelledby="inventory-heading">
      <div className={styles.filters}>
        <h1 id="inventory-heading" className="visually-hidden">
          Inventaire
        </h1>

        <div className={styles.searchRow}>
          <label className="visually-hidden" htmlFor="inventory-search">
            Rechercher un article
          </label>
          <input
            id="inventory-search"
            className={styles.search}
            type="search"
            inputMode="search"
            placeholder="Rechercher un article…"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
            }}
          />
        </div>

        <ChipRow label="Filtrer par emplacement">
          <Chip
            active={locationId === null}
            onClick={() => {
              setLocationId(null);
            }}
          >
            Tous
          </Chip>
          {locations.map((location) => (
            <Chip
              key={location.id}
              active={locationId === location.id}
              onClick={() => {
                setLocationId(location.id);
              }}
            >
              {location.name} ({location.item_count})
            </Chip>
          ))}
        </ChipRow>

        <ChipRow label="Filtrer par péremption">
          <Chip
            active={expiringOnly}
            onClick={() => {
              setExpiringOnly((value) => !value);
            }}
          >
            Périme sous {SOON_DAYS} jours
          </Chip>
        </ChipRow>
      </div>

      {locationsError ? (
        <p className={styles.summary} role="status">
          Emplacements indisponibles : {locationsError}
        </p>
      ) : null}

      <p className={styles.summary} aria-live="polite" aria-busy={loading}>
        {loading
          ? 'Chargement de l’inventaire…'
          : error
            ? 'Inventaire indisponible.'
            : `${String(total)} article${total > 1 ? 's' : ''} ${filtered ? 'correspondants' : 'en stock'}.`}
      </p>

      {loading && items.length === 0 ? (
        <LoadingRow label="Chargement de l’inventaire…" />
      ) : error ? (
        <ErrorState
          message={error}
          onRetry={() => {
            setReloadNonce((value) => value + 1);
          }}
        />
      ) : items.length === 0 ? (
        filtered ? (
          <EmptyState
            title="Aucun article ne correspond"
            body="Modifiez la recherche ou retirez les filtres pour voir tout le stock."
            action={
              <Button variant="secondary" onClick={resetFilters}>
                Réinitialiser les filtres
              </Button>
            }
          />
        ) : (
          <EmptyState
            title="Votre stock est vide"
            body="Scannez un code-barres ou saisissez un article à la main pour commencer."
            action={
              <Button variant="primary" onClick={onAddItem}>
                Ajouter un article
              </Button>
            }
          />
        )
      ) : (
        <div className={styles.groups}>
          {groups.map((group) => (
            <section key={group.id} className={styles.group} aria-label={group.name}>
              <header className={styles.groupHeader}>
                <h2 className={styles.groupName}>{group.name}</h2>
                <span className={styles.groupKind}>{group.kindLabel}</span>
              </header>
              <ul className={styles.list}>
                {group.items.map((item) => (
                  <InventoryItemRow key={item.id} item={item} onRemove={removeItem} />
                ))}
              </ul>
            </section>
          ))}

          {items.length < total ? (
            <div className={styles.footer}>
              <Button variant="secondary" loading={loadingMore} onClick={loadMore}>
                Charger plus ({String(items.length)} / {String(total)})
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}
