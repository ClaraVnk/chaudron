/**
 * Reading a date somebody typed, rather than one they picked.
 *
 * `<input type="date">` is accessible, locale-aware and validated, and on a
 * phone it is a calendar. Putting away twenty items means twenty calendars for
 * dates that are nearly always within the next fortnight and are printed on the
 * packet in front of you — so typing them is faster, and the picker is the slow
 * path rather than the only one.
 *
 * What this accepts is deliberately narrow. A date is not a place to be clever:
 * a parser that guesses turns `03/04` into April or March depending on where it
 * thinks you are, and the household finds out when the yoghurt is thrown away a
 * month early. Everything here is day-first, which is what the printed date on
 * French packaging is, and anything ambiguous is refused rather than resolved.
 */

/** ISO `YYYY-MM-DD`, the only shape the API accepts. */
export type IsoDate = string;

const SEPARATORS = /[\s./-]+/;

function isRealDate(year: number, month: number, day: number): boolean {
  // `Date` rolls over silently — 31 February becomes 3 March — so the only way
  // to reject an impossible date is to build one and check it kept its parts.
  const probe = new Date(Date.UTC(year, month - 1, day));
  return (
    probe.getUTCFullYear() === year &&
    probe.getUTCMonth() === month - 1 &&
    probe.getUTCDate() === day
  );
}

/**
 * Expand a two-digit year against the current century.
 *
 * `26` is 2026, not 1926: every date this application stores is an expiry, and
 * an expiry in the past century is not a date somebody meant to type. The
 * window is deliberately wide on the future side — a tin of beans can carry a
 * date five years out — and narrow on the past, where only the last year is
 * plausible (a use-by that has just gone).
 */
function expandYear(value: number, today: Date): number {
  if (value >= 100) return value;
  const century = Math.floor(today.getUTCFullYear() / 100) * 100;
  const candidate = century + value;
  return candidate < today.getUTCFullYear() - 1 ? candidate + 100 : candidate;
}

/**
 * Parse a typed date, or return `null`.
 *
 * Accepted, all day-first:
 *
 *   `12/08/2026`  `12-08-2026`  `12.08.2026`  `12 08 2026`
 *   `12/08/26`    `120826`      `12082026`
 *   `2026-08-12`  — ISO, because it is what the field round-trips
 *
 * Refused: anything else, including a bare `12/08`. Inferring the year from
 * "soon" is the kind of helpfulness that puts a use-by in the wrong year, and
 * the cost of being wrong here is food thrown away or eaten late.
 */
export function parseTypedDate(input: string, today: Date = new Date()): IsoDate | null {
  const trimmed = input.trim();
  if (trimmed === '') return null;

  // ISO first: it is unambiguous, and it is what the input round-trips when the
  // household used the picker instead.
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
  if (iso) {
    const [, y, m, d] = iso;
    const year = Number(y);
    const month = Number(m);
    const day = Number(d);
    return isRealDate(year, month, day) ? trimmed : null;
  }

  let day: number;
  let month: number;
  let year: number;

  const digitsOnly = /^\d+$/.test(trimmed);
  if (digitsOnly) {
    // `120826` or `12082026`. Any other length is a typo, not a date — and
    // padding or truncating one would invent a value nobody typed.
    if (trimmed.length === 6) {
      day = Number(trimmed.slice(0, 2));
      month = Number(trimmed.slice(2, 4));
      year = Number(trimmed.slice(4, 6));
    } else if (trimmed.length === 8) {
      day = Number(trimmed.slice(0, 2));
      month = Number(trimmed.slice(2, 4));
      year = Number(trimmed.slice(4, 8));
    } else {
      return null;
    }
  } else {
    const parts = trimmed.split(SEPARATORS).filter((part) => part !== '');
    if (parts.length !== 3) return null;
    if (!parts.every((part) => /^\d{1,4}$/.test(part))) return null;
    day = Number(parts[0]);
    month = Number(parts[1]);
    year = Number(parts[2]);
  }

  year = expandYear(year, today);
  if (!isRealDate(year, month, day)) return null;

  const pad = (value: number) => String(value).padStart(2, '0');
  return `${String(year)}-${pad(month)}-${pad(day)}`;
}

/** Render an ISO date the way it is typed here, for showing back what was read. */
export function formatTypedDate(iso: IsoDate): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!match) return iso;
  const [, y, m, d] = match;
  return `${d}/${m}/${y}`;
}
