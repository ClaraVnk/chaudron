import type { ExpiryKind, LocationKind } from '../api/types';

export type ExpiryLevel = 'expired' | 'critical' | 'soon' | 'ok' | 'none';

/** Days until expiry, computed on calendar days rather than elapsed hours. */
export function daysUntil(isoDate: string | null, today = new Date()): number | null {
  if (!isoDate) return null;
  const parts = isoDate.slice(0, 10).split('-').map(Number);
  const [year, month, day] = parts;
  if (year === undefined || month === undefined || day === undefined) return null;
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return null;

  const target = Date.UTC(year, month - 1, day);
  const now = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((target - now) / 86_400_000);
}

export const CRITICAL_DAYS = 2;
export const SOON_DAYS = 7;

export function expiryLevel(isoDate: string | null, today = new Date()): ExpiryLevel {
  const days = daysUntil(isoDate, today);
  if (days === null) return 'none';
  if (days < 0) return 'expired';
  if (days <= CRITICAL_DAYS) return 'critical';
  if (days <= SOON_DAYS) return 'soon';
  return 'ok';
}

const dateFormatter = new Intl.DateTimeFormat('fr-FR', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
});

export function formatDate(isoDate: string): string {
  const parts = isoDate.slice(0, 10).split('-').map(Number);
  const [year, month, day] = parts;
  if (year === undefined || month === undefined || day === undefined) return isoDate;
  return dateFormatter.format(new Date(year, month - 1, day));
}

/** Short, scannable label: "périmé depuis 3 j", "demain", "dans 5 j". */
export function formatExpiryRelative(isoDate: string | null, today = new Date()): string | null {
  const days = daysUntil(isoDate, today);
  if (days === null) return null;
  if (days < -1) return `périmé depuis ${String(-days)} j`;
  if (days === -1) return 'périmé depuis hier';
  if (days === 0) return "périme aujourd'hui";
  if (days === 1) return 'périme demain';
  if (days <= SOON_DAYS) return `dans ${String(days)} j`;
  return null;
}

export function expiryKindLabel(kind: ExpiryKind | null): string {
  switch (kind) {
    case 'use_by':
      return 'À consommer jusqu’au';
    case 'best_before':
      return 'À consommer de préférence avant le';
    default:
      return 'Date';
  }
}

export function expiryKindShortLabel(kind: ExpiryKind | null): string {
  switch (kind) {
    case 'use_by':
      return 'DLC';
    case 'best_before':
      return 'DDM';
    default:
      return 'Date';
  }
}

const LOCATION_LABELS: Record<LocationKind, string> = {
  fridge: 'Réfrigérateur',
  freezer: 'Congélateur',
  pantry: 'Placard',
  cellar: 'Cave',
  other: 'Autre',
};

export function locationKindLabel(kind: LocationKind): string {
  return LOCATION_LABELS[kind] ?? 'Autre';
}

/** Quantities arrive as decimal strings; trim trailing zeros without parsing. */
export function formatAmount(amount: string): string {
  if (!amount.includes('.')) return amount;
  const trimmed = amount.replace(/0+$/, '').replace(/\.$/, '');
  return trimmed === '' ? '0' : trimmed;
}
