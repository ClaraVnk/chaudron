import { useState } from 'react';
import { describeError } from '../../api/client';
import { freezeInventoryItem, thawInventoryItem } from '../../api/endpoints';
import type {
  FrozenInventoryItem,
  InventoryItem,
  RemovalReason,
  StorageLocation,
  UpdatedInventoryItem,
} from '../../api/types';
import { Badge, Button } from '../../components/ui';
import { unitSymbol } from '../../lib/units';
import {
  expiryKindShortLabel,
  expiryLevel,
  formatAmount,
  formatDate,
  formatExpiryRelative,
} from '../../lib/expiry';
import { QuantityAdjuster } from './QuantityAdjuster';
import styles from './Inventory.module.css';

interface Props {
  item: InventoryItem;
  /** Forwarded to the adjuster so a misfiled lot can be moved. */
  locations: StorageLocation[];
  onRemove: (id: string, reason: RemovalReason) => Promise<void>;
  onAdjusted: (item: UpdatedInventoryItem) => void;
  onFrozen: (item: FrozenInventoryItem, message: string) => void;
}

type Mode = 'idle' | 'removing' | 'adjusting' | 'freezing';

/** Frozen, and not since thawed — the same predicate the server indexes on. */
function isFrozen(item: InventoryItem): boolean {
  return item.frozen_at !== null && item.thawed_at === null;
}

/**
 * What the interface says about *where the lot went*, in French.
 *
 * The server answers with a code and not a sentence, deliberately: three of the
 * four are things a user has to be told in their own language, and choosing that
 * wording is this layer's job. `moved` says nothing here — the row already shows
 * the new location, and repeating it would be noise on the common case.
 */
function locationSentence(answer: FrozenInventoryItem): string | null {
  switch (answer.location_change) {
    case 'unresolved':
      return 'Aucun emplacement n’a été modifié : créez un congélateur, ou choisissez entre les vôtres.';
    case 'occupied':
      return 'L’article est resté à sa place : un lot identique occupe déjà le congélateur.';
    default:
      return null;
  }
}

export function InventoryItemRow({ item, locations, onRemove, onAdjusted, onFrozen }: Props) {
  const [mode, setMode] = useState<Mode>('idle');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const frozen = isFrozen(item);
  const thawed = item.thawed_at !== null;

  /**
   * Urgency is read off `effective_expires_on`, never off the printed date.
   *
   * They are the same thing for most of an inventory and wildly different for the
   * lots this row most needs to get right: a chicken breast frozen the day before
   * its use-by would otherwise be painted red and sorted to the top of a list the
   * server had put it at the bottom of.
   */
  const governing = item.effective_expires_on;
  const level = expiryLevel(governing);
  const relative = formatExpiryRelative(governing);
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
      setMode('idle');
    });
  };

  const runFreeze = (action: 'freeze' | 'thaw') => {
    setBusy(true);
    setFailure(null);
    const call = action === 'freeze' ? freezeInventoryItem : thawInventoryItem;
    void call(item.id)
      .then((answer) => {
        const verb = action === 'freeze' ? 'congelé' : 'décongelé';
        const where = locationSentence(answer);
        // "No date proposed" is not "keeps indefinitely", and the note is the only
        // thing that says which of the two it is. Reported here rather than left
        // to the badge, because a badge that reads "sans date" after a freeze is
        // read as a bug.
        const undated =
          action === 'freeze' && !answer.proposes_expiry_date
            ? 'Aucune date n’est proposée pour cette famille.'
            : null;
        onFrozen(
          answer,
          [`${item.product.name} ${verb}.`, undated, where].filter(Boolean).join(' '),
        );
        setNotice([undated, where].filter(Boolean).join(' ') || null);
        setMode('idle');
      })
      .catch((cause: unknown) => {
        // Shown on the row rather than swallowed: every refusal the server sends
        // back is a food-safety sentence — already past its date, already thawed —
        // and it is the reason the user is standing at an open freezer.
        setFailure(describeError(cause));
        setMode('idle');
      })
      .finally(() => {
        setBusy(false);
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
          {unitSymbol(item.quantity.unit)}
        </span>
      </div>

      <div className={styles.itemMeta}>
        {governing ? (
          <>
            <span>
              {frozen || thawed ? 'À consommer avant le' : expiryKindShortLabel(item.expiry_kind)}{' '}
              {formatDate(governing)}
            </span>
            {relative ? (
              <Badge tone={level === 'expired' ? 'danger' : 'warn'}>{relative}</Badge>
            ) : null}
          </>
        ) : (
          <span>Sans date</span>
        )}
        {frozen ? <Badge tone="ok">congelé</Badge> : null}
        {thawed ? <Badge tone="warn">décongelé — 3 j</Badge> : null}
        {item.opened_at ? <Badge tone="neutral">entamé</Badge> : null}
      </div>

      {/*
        The printed date, kept visible whenever freezing has replaced it. It is
        what is written on the pack, and hiding it would make the row look as if
        the application had invented the date above.
      */}
      {(frozen || thawed) && item.expires_on ? (
        <p className={styles.itemFootnote}>
          {expiryKindShortLabel(item.expiry_kind)} imprimée : {formatDate(item.expires_on)}
        </p>
      ) : null}

      {notice ? (
        <p className={styles.itemFootnote} role="status">
          {notice}
        </p>
      ) : null}
      {failure ? (
        <p className={styles.itemFailure} role="alert">
          {failure}
        </p>
      ) : null}

      {mode === 'adjusting' ? (
        <QuantityAdjuster
          item={item}
          locations={locations}
          onSaved={(updated) => {
            onAdjusted(updated);
            setMode('idle');
          }}
          onCancel={() => {
            setMode('idle');
          }}
        />
      ) : mode === 'freezing' ? (
        <div className={styles.itemActions}>
          {/*
            The advice arrives before the door closes, not with the answer. For
            two families it says "do not do this" — a shell egg cracks, a sealed
            tin bursts — and the application records the freeze anyway: it is a
            fact about the household's own freezer, and refusing to know it is
            worse than knowing it.
          */}
          {item.freezing_note ? <p className={styles.itemFootnote}>{item.freezing_note}</p> : null}
          <Button
            variant="primary"
            loading={busy}
            onClick={() => {
              runFreeze('freeze');
            }}
          >
            Confirmer la congélation
            <span className="visually-hidden"> de {item.product.name}</span>
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setMode('idle');
            }}
          >
            Annuler
          </Button>
        </div>
      ) : mode === 'removing' ? (
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
              setMode('idle');
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
              setMode('adjusting');
            }}
          >
            Ajuster
            <span className="visually-hidden"> la quantité de {item.product.name}</span>
          </Button>
          {/*
            Never offered on a lot that has been thawed: it is the ANSES rule, the
            server refuses it, and an interface that offers what it knows will be
            refused teaches the user to ignore the refusal when it matters.
          */}
          {frozen ? (
            <Button
              variant="ghost"
              loading={busy}
              onClick={() => {
                runFreeze('thaw');
              }}
            >
              Décongeler
              <span className="visually-hidden"> {item.product.name}</span>
            </Button>
          ) : thawed ? null : (
            <Button
              variant="ghost"
              onClick={() => {
                setMode('freezing');
              }}
            >
              Congeler
              <span className="visually-hidden"> {item.product.name}</span>
            </Button>
          )}
          <Button
            variant="ghost"
            onClick={() => {
              setMode('removing');
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
