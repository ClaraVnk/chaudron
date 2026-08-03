import { useId, type ButtonHTMLAttributes, type ReactNode } from 'react';
import styles from './ui.module.css';

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  block?: boolean;
  loading?: boolean;
}

export function Button({
  variant = 'secondary',
  block = false,
  loading = false,
  children,
  className,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  const classes = [styles.button, styles[variant], block ? styles.block : '', className ?? '']
    .filter(Boolean)
    .join(' ');
  return (
    <button
      type={type}
      className={classes}
      disabled={disabled ?? loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span className={styles.spinner} aria-hidden="true" /> : null}
      {children}
    </button>
  );
}

interface FieldProps {
  label: string;
  hint?: string;
  error?: string | null;
  required?: boolean;
  children: (props: { id: string; describedBy: string | undefined; invalid: boolean }) => ReactNode;
}

/**
 * Renders a real <label for> bound to the control, plus a described-by chain for
 * hint and error text. The render-prop shape exists so the id can never drift
 * between the label and the input.
 */
export function Field({ label, hint, error, required = false, children }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy =
    [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(' ') || undefined;

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
        {required ? <span aria-hidden="true"> *</span> : null}
        {required ? <span className="visually-hidden"> (obligatoire)</span> : null}
      </label>
      {children({ id, describedBy, invalid: Boolean(error) })}
      {hint ? (
        <p className={styles.hint} id={hintId}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className={styles.fieldError} id={errorId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}

interface ChipProps {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
  /** Chips act as a single-select group, so they carry aria-pressed. */
  label?: string;
}

export function Chip({ active, onClick, children, label }: ChipProps) {
  return (
    <button
      type="button"
      className={[styles.chip, active ? styles.chipActive : ''].filter(Boolean).join(' ')}
      aria-pressed={active}
      aria-label={label}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

export function ChipRow({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className={styles.chipRow} role="group" aria-label={label}>
      {children}
    </div>
  );
}

type BadgeTone = 'neutral' | 'warn' | 'danger' | 'ok';

export function Badge({ tone = 'neutral', children }: { tone?: BadgeTone; children: ReactNode }) {
  const toneClass = {
    neutral: styles.badgeNeutral,
    warn: styles.badgeWarn,
    danger: styles.badgeDanger,
    ok: styles.badgeOk,
  }[tone];
  return <span className={[styles.badge, toneClass].join(' ')}>{children}</span>;
}

type CalloutTone = 'info' | 'warn' | 'danger';

export function Callout({
  tone = 'info',
  title,
  children,
}: {
  tone?: CalloutTone;
  title?: string;
  children: ReactNode;
}) {
  const toneClass = {
    info: styles.calloutInfo,
    warn: styles.calloutWarn,
    danger: styles.calloutDanger,
  }[tone];
  return (
    <div className={[styles.callout, toneClass].join(' ')}>
      {title ? <p className={styles.calloutTitle}>{title}</p> : null}
      {children}
    </div>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className={styles.state}>
      <p className={styles.stateTitle}>{title}</p>
      <p className={styles.stateBody}>{body}</p>
      {action}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className={styles.state} role="alert">
      <p className={styles.stateTitle}>Échec du chargement</p>
      <p className={styles.stateBody}>{message}</p>
      {onRetry ? (
        <Button variant="secondary" onClick={onRetry}>
          Réessayer
        </Button>
      ) : null}
    </div>
  );
}

export function LoadingRow({ label }: { label: string }) {
  return (
    <p className={styles.loadingRow}>
      <span className={styles.spinner} aria-hidden="true" />
      {label}
    </p>
  );
}
