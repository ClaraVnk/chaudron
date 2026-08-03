/**
 * GTIN helpers. All of this runs client-side on purpose: it removes network
 * round-trips that are known in advance to fail.
 */

export function normaliseGtin(input: string): string {
  return input.replace(/\D/g, '');
}

/**
 * GS1 mod-10 check digit, valid for GTIN-8, GTIN-12, GTIN-13 and GTIN-14.
 * Catches a typo in manual entry before it becomes a misleading 404.
 */
export function hasValidGtinChecksum(gtin: string): boolean {
  const digits = normaliseGtin(gtin);
  if (![8, 12, 13, 14].includes(digits.length)) return false;

  let sum = 0;
  // Weights alternate 3, 1, 3, 1… walking left from the check digit: the digit
  // immediately before the check digit is weighted 3.
  for (let i = digits.length - 2; i >= 0; i -= 1) {
    const digit = Number(digits[i]);
    const distanceFromCheck = digits.length - 1 - i;
    const weight = distanceFromCheck % 2 === 1 ? 3 : 1;
    sum += digit * weight;
  }
  const expected = (10 - (sum % 10)) % 10;
  return expected === Number(digits[digits.length - 1]);
}

/**
 * GS1 Restricted Circulation Numbers: prefixes `02` and `20`–`29`.
 *
 * Retailers assign these internally to anything weighed in store (butchery,
 * cheese counter, loose fruit). They encode a price or a weight in a
 * chain-specific layout, they will never appear in a public product database,
 * and the same product carries a different code on every package. Detecting
 * them here means we never call the API and never poison a cache with codes
 * that are unique per package.
 *
 * See docs/technical-notes-scanning.md §4.4.
 */
export function isRestrictedCirculationNumber(gtin: string): boolean {
  const digits = normaliseGtin(gtin);
  if (digits.length < 2) return false;
  const prefix = digits.slice(0, 2);
  return prefix === '02' || /^2[0-9]$/.test(prefix);
}

export function isPlausibleGtin(gtin: string): boolean {
  const digits = normaliseGtin(gtin);
  return [8, 12, 13, 14].includes(digits.length);
}
