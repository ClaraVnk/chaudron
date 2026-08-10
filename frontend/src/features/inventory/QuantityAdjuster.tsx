import { useId, useState } from 'react';
import { describeError } from '../../api/client';
import { updateInventoryItem } from '../../api/endpoints';
import type { InventoryItem, InventoryItemPatch, StorageLocation, UpdatedInventoryItem } from '../../api/types';
import { Button } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { formatAmount } from '../../lib/expiry';
import { divideAmount, normaliseAmount, stepAmount } from '../../lib/quantity';
import { unitSymbol } from '../../lib/units';
import styles from './Inventory.module.css';

interface Props {
  item: InventoryItem;
  /** Every active location, so a lot filed in the wrong one can be moved. */
  locations: StorageLocation[];
  onSaved: (item: UpdatedInventoryItem) => void;
  onCancel: () => void;
}

/**
 * Inline quantity correction, reachable from the row itself.
 *
 * The common gesture is "there is half left", not "let me re-edit the record",
 * so the two shortcuts come first and the text field second.
 *
 * The unit is never sent. `PATCH` only carries `amount`, which makes it
 * impossible for a conversion to slip in: someone who typed "1 L" reads back
 * "1 L", never "1000 ml" (contract v1.1 §6).
 */
export function QuantityAdjuster({ item, locations, onSaved, onCancel }: Props) {
  const inputId = useId();
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;
  const [value, setValue] = useState(formatAmount(item.quantity.amount));
  // A lot filed in the wrong place had no way back: the row offered quantity,
  // freezing and removal, and `PATCH` accepted a location the whole time. A
  // wrong location is not cosmetic — the freezer suspends the expiry date, so
  // an item put there by mistake stops being counted as perishable.
  const [locationId, setLocationId] = useState(item.location?.id ?? '');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const apply = (next: string | null) => {
    if (next === null) return;
    setValue(next);
    setError(null);
  };

  const zeroed = value.trim() === '0';

  const save = () => {
    const amount = zeroed ? '0' : normaliseAmount(value);
    if (amount === null) {
      setError('Indiquez une quantité positive, par exemple 0,5.');
      return;
    }

    setSaving(true);
    setError(null);
    // Only what changed. Sending an unchanged location would be harmless today
    // and is still wrong: a PATCH that names a field claims the user set it,
    // and the server's audit trail reads it that way.
    const patch: InventoryItemPatch = { amount };
    if (locationId !== '' && locationId !== (item.location?.id ?? '')) {
      patch.location_id = locationId;
    }
    updateInventoryItem(item.id, patch)
      .then(onSaved)
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  return (
    <div className={styles.adjuster}>
      <div className={styles.adjusterShortcuts}>
        <Button
          variant="secondary"
          onClick={() => {
            apply(divideAmount(item.quantity.amount, 2));
          }}
        >
          Il en reste la moitié
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            apply(divideAmount(item.quantity.amount, 4));
          }}
        >
          Un quart
        </Button>
      </div>

      {locations.length > 1 ? (
        <div className={styles.adjusterField}>
          <label className={styles.adjusterLabel} htmlFor={`${inputId}-location`}>
            Emplacement
          </label>
          <select
            id={`${inputId}-location`}
            className={controlClass()}
            value={locationId}
            onChange={(event) => {
              setLocationId(event.target.value);
            }}
          >
            {locations.map((location) => (
              <option key={location.id} value={location.id}>
                {location.name}
              </option>
            ))}
          </select>
        </div>
      ) : null}

      <div className={styles.adjusterRow}>
        <Button
          variant="secondary"
          aria-label="Retirer une unité"
          onClick={() => {
            apply(stepAmount(value, -1));
          }}
        >
          −
        </Button>

        <div className={styles.adjusterField}>
          <label className={styles.adjusterLabel} htmlFor={inputId}>
            Quantité en {unitSymbol(item.quantity.unit)}
          </label>
          <input
            id={inputId}
            className={controlClass(error !== null)}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            aria-describedby={error === null ? hintId : `${hintId} ${errorId}`}
            aria-invalid={error !== null}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
              setError(null);
            }}
          />
        </div>

        <Button
          variant="secondary"
          aria-label="Ajouter une unité"
          onClick={() => {
            apply(stepAmount(value, 1));
          }}
        >
          +
        </Button>
      </div>

      <p className={styles.adjusterHint} id={hintId}>
        L’unité « {unitSymbol(item.quantity.unit)} » est conservée telle quelle. Un ajustement
        manuel est enregistré comme correction, pas comme consommation.
      </p>

      {zeroed ? (
        <p className={styles.adjusterHint}>
          Une correction à zéro ne compte pas comme un produit terminé. S’il est fini, utilisez «
          Retirer du stock » : c’est ce geste qui peut proposer de le racheter.
        </p>
      ) : null}

      {error !== null ? (
        <p className={styles.adjusterError} id={errorId} role="alert">
          {error}
        </p>
      ) : null}

      <div className={styles.itemActions}>
        <Button variant="ghost" onClick={onCancel}>
          Annuler
        </Button>
        <Button variant="primary" loading={saving} onClick={save}>
          Enregistrer
        </Button>
      </div>
    </div>
  );
}
