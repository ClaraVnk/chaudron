import { getActiveHouseholdId } from '../api/session';
import type { MealTemperature } from '../api/types';

/**
 * The last meal-temperature choice, remembered per household.
 *
 * This is the only thing the app persists locally, and deliberately so: it is a
 * taste, not health data. Nothing about members, allergens or age bands is ever
 * written here.
 *
 * There is no inference from season or weather either. Guessing "it is hot,
 * suggest a salad" would mean knowing where the household lives — a datum this
 * product has no reason to collect (contract v1.1 §4ter).
 */

const KEY = 'chaudron.meal-temperature';

function storageKey(): string | null {
  // The active household is runtime state now, not a build constant: one build
  // serves every household, and an account may switch between two of them.
  const householdId = getActiveHouseholdId();
  return householdId === null ? null : `${KEY}.${householdId}`;
}

export function readMealTemperature(): MealTemperature {
  const key = storageKey();
  if (key === null) return 'any';
  try {
    const raw = window.localStorage.getItem(key);
    return raw === 'hot' || raw === 'cold' ? raw : 'any';
  } catch {
    // Private browsing and blocked storage both throw. A forgotten preference
    // is not worth a broken screen.
    return 'any';
  }
}

export function writeMealTemperature(value: MealTemperature): void {
  const key = storageKey();
  if (key === null) return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* ignored, see above */
  }
}

const LOCATION_KEY = 'chaudron.last-location';

function locationStorageKey(): string | null {
  const householdId = getActiveHouseholdId();
  return householdId === null ? null : `${LOCATION_KEY}.${householdId}`;
}

/**
 * The location the household last put something in.
 *
 * The add form used to default to `locations[0]`, which is the first row by
 * sort order and has nothing to do with what anybody is holding. For a
 * household whose freezer happens to sort first, that meant every item landed
 * in the freezer unless corrected — twenty times per shopping trip, and a
 * mistake that also suspends the expiry date, so it is not merely tedious.
 *
 * Remembering the last choice is not a guess about intent: groceries are put
 * away in runs, and the previous answer is a far better prior than an arbitrary
 * row. The caller still falls back to the first location when there is nothing
 * remembered, or when what was remembered has since been archived.
 */
export function readLastLocationId(): string | null {
  const key = locationStorageKey();
  if (key === null) return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeLastLocationId(id: string): void {
  const key = locationStorageKey();
  if (key === null) return;
  try {
    window.localStorage.setItem(key, id);
  } catch {
    /* ignored, see above */
  }
}
