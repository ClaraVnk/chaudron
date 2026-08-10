import { useId, useState } from 'react';
import { controlClass } from './controlClass';
import { formatTypedDate, parseTypedDate } from '../lib/typedDate';
import styles from './ExpiryDateInput.module.css';

interface Props {
  /** ISO `YYYY-MM-DD`, or `''` for no date. */
  value: string;
  onChange: (iso: string) => void;
  id?: string;
  describedBy?: string;
}

/**
 * An expiry date you can type, with the calendar still there.
 *
 * `<input type="date">` alone is a calendar on a phone, and putting away twenty
 * items means twenty calendars for dates that are printed on the packet in
 * front of you and are nearly always within a fortnight. Typing is the fast
 * path; the picker stays for the cases where a calendar genuinely helps — "the
 * Thursday after next" is easier to point at than to work out.
 *
 * The two halves are one value. Typing updates the picker, picking updates the
 * text, and the parent only ever sees ISO — so nothing downstream has to know
 * this field accepts anything else.
 *
 * What is refused is as important as what is accepted: see `lib/typedDate.ts`.
 * A half-typed date is not an error, it is somebody mid-keystroke, so the
 * message only appears once the field is left.
 */
export function ExpiryDateInput({ value, onChange, id, describedBy }: Props) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const pickerId = `${inputId}-picker`;
  const errorId = `${inputId}-typed-error`;

  const [text, setText] = useState(() => (value === '' ? '' : formatTypedDate(value)));
  const [touched, setTouched] = useState(false);

  // The picker, a reset, or a parent that reformats all write `value` behind
  // this component's back, and the two halves have to stay one value rather
  // than two that drift.
  //
  // Adjusted during render rather than in an effect. React documents this
  // exact case — state derived from a prop that changed — and it is what
  // `react-hooks/set-state-in-effect` exists to push you towards: an effect
  // would render the stale text once, then re-render, so the household would
  // briefly see the previous date.
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) {
    setLastValue(value);
    setText(value === '' ? '' : formatTypedDate(value));
  }

  const unreadable = touched && text.trim() !== '' && parseTypedDate(text) === null;

  return (
    <div className={styles.row}>
      <input
        id={inputId}
        aria-describedby={[describedBy, unreadable ? errorId : null].filter(Boolean).join(' ') || undefined}
        aria-invalid={unreadable || undefined}
        className={controlClass()}
        type="text"
        // `numeric`, not `decimal`: a date has no separator worth a dedicated
        // key, and this is the keypad that opens on a phone.
        inputMode="numeric"
        autoComplete="off"
        placeholder="JJ/MM/AAAA"
        value={text}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const parsed = parseTypedDate(next);
          // Only propagate something readable. Clearing the field clears the
          // date; a half-typed one leaves the last good value alone rather than
          // wiping it on every keystroke.
          if (parsed !== null) onChange(parsed);
          else if (next.trim() === '') onChange('');
        }}
        onBlur={() => {
          setTouched(true);
          const parsed = parseTypedDate(text);
          // Normalise on leaving, so `120826` reads back as `12/08/2026` and
          // the household can see what was understood.
          if (parsed !== null) setText(formatTypedDate(parsed));
        }}
      />

      <label className={styles.pickerLabel} htmlFor={pickerId}>
        <span className="visually-hidden">Choisir la date dans un calendrier</span>
        <span aria-hidden="true">📅</span>
      </label>
      <input
        id={pickerId}
        className={styles.picker}
        type="date"
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          setTouched(false);
        }}
      />

      {unreadable ? (
        <p id={errorId} className={styles.error}>
          Date non comprise. Tapez par exemple 12/08/2026, ou 120826.
        </p>
      ) : null}
    </div>
  );
}
