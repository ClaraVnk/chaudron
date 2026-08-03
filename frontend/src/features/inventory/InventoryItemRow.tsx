import { useState } from 'react';
import type { InventoryItem, RemovalReason } from '../../api/types';
import { Badge, Button } from '../../components/ui';
import {
  expiryKindShortLabel,
  expiryLevel,
  formatAmount,
  formatDate,
  formatExpiryRelative,
} from '../../lib/expiry';
import styles from './Inventory.module.css';

interface Props {
  item: InventoryItem;
  onRemove: (id: string, reason: RemovalReason) => Promise<void>;
}

export function InventoryItemRow({ item, onRemove }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const level = expiryLevel(item.expires_on);
  const relative = formatExpiryRelative(item.expires_on);
  const rowClass = [
    styles.item,
    level === 'expired' ? styles.itemExpired : '',
    level === 'critical' || level === 'soon' ? styles.itemExpiring : '',
  ]
    .filter(Boolean)
    .join(' ');

  const remove = (reason: RemovalReason) => {
    setBusy(true);
    void onRemove(item.id, reason).finally(() => {
      setBusy(false);
      setConfirming(false);
    });
  };

  return (
    <li className={rowClass}>
      <div className={styles.itemMain}>
        <div className={styles.itemText}>
          <span className={styles.itemName}>{item.product.name}</span>
          {item.product.brand ? (
            <span className={styles.itemBrand}>{item.product.brand}</span>
          ) : null}
        </div>
        <span className={styles.itemQuantity}>
          {formatAmount(item.quantity.amount)}
          <span aria-hidden="true"> </span>
          {item.quantity.unit}
        </span>
      </div>

      <div className={styles.itemMeta}>
        {item.expires_on ? (
          <>
            <span>
              {expiryKindShortLabel(item.expiry_kind)} {formatDate(item.expires_on)}
            </span>
            {relative ? (
              <Badge tone={level === 'expired' ? 'danger' : 'warn'}>{relative}</Badge>
            ) : null}
          </>
        ) : (
          <span>Sans date</span>
        )}
        {item.opened_at ? <Badge tone="neutral">entamé</Badge> : null}
      </div>

      {confirming ? (
        <div className={styles.itemActions}>
          <Button
            variant="secondary"
            loading={busy}
            onClick={() => {
              remove('consumed');
            }}
          >
            Consommé
          </Button>
          <Button
            variant="danger"
            loading={busy}
            onClick={() => {
              remove('wasted');
            }}
          >
            Jeté
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setConfirming(false);
            }}
          >
            Annuler
          </Button>
        </div>
      ) : (
        <div className={styles.itemActions}>
          <Button
            variant="ghost"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Retirer du stock
            <span className="visually-hidden"> — {item.product.name}</span>
          </Button>
        </div>
      )}
    </li>
  );
}
