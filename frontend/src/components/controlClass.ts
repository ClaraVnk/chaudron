import styles from './ui.module.css';

/**
 * Shared class for native form controls. Lives outside `ui.tsx` so that file
 * exports components only and stays fast-refresh friendly.
 */
export function controlClass(invalid = false): string {
  return [styles.control, invalid ? styles.invalid : ''].filter(Boolean).join(' ');
}
