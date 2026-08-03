import { useCallback, useState } from 'react';
import { configError } from './api/config';
import { DegradedBanner } from './components/DegradedBanner';
import { CapabilitiesProvider } from './context/CapabilitiesProvider';
import { AddItemScreen } from './features/add/AddItemScreen';
import { InventoryScreen } from './features/inventory/InventoryScreen';
import { RecipesScreen } from './features/recipes/RecipesScreen';
import { useLocations } from './hooks/useLocations';
import styles from './components/AppShell.module.css';

type Tab = 'inventory' | 'add' | 'recipes';

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: 'inventory', label: 'Inventaire', icon: '▦' },
  { id: 'add', label: 'Ajouter', icon: '＋' },
  { id: 'recipes', label: 'Recettes', icon: '🍲' },
];

/**
 * Configuration is validated at module load; without it every call would fail
 * with an opaque network error. One honest screen beats a stream of them.
 */
function ConfigurationScreen({ message }: { message: string }) {
  return (
    <main className={styles.fatal}>
      <h1>Chaudron n’est pas configuré</h1>
      <p>{message}</p>
      <p>
        Copiez <code>.env.example</code> vers <code>.env.local</code> dans <code>frontend/</code>,
        renseignez <code>VITE_API_BASE_URL</code> et <code>VITE_HOUSEHOLD_ID</code>, puis relancez
        la construction.
      </p>
    </main>
  );
}

function Shell() {
  const [tab, setTab] = useState<Tab>('inventory');
  const [inventoryVersion, setInventoryVersion] = useState(0);
  const { locations, error: locationsError, reload: reloadLocations } = useLocations();

  const handleInventoryChanged = useCallback(() => {
    setInventoryVersion((value) => value + 1);
    // Item counts live on the locations payload, so they go stale too.
    reloadLocations();
  }, [reloadLocations]);

  return (
    <div className={styles.shell}>
      <a className={styles.skipLink} href="#main">
        Aller au contenu
      </a>

      <header className={styles.header}>
        <img className={styles.logo} src="/icon-192.png" alt="" width={32} height={32} />
        <span className={styles.wordmark}>Chaudron</span>
        {/* Brand tagline, verbatim from assets/logo.svg — brand copy, not UI copy. */}
        <span className={styles.tagline}>Throw in what you have. See what comes out.</span>
      </header>

      <DegradedBanner />

      <main className={styles.main} id="main" tabIndex={-1}>
        {tab === 'inventory' ? (
          <InventoryScreen
            locations={locations}
            locationsError={locationsError}
            refreshToken={inventoryVersion}
            onChanged={handleInventoryChanged}
            onAddItem={() => {
              setTab('add');
            }}
          />
        ) : null}

        {tab === 'add' ? (
          <AddItemScreen
            locations={locations}
            locationsError={locationsError}
            onAdded={handleInventoryChanged}
            onGoToInventory={() => {
              setTab('inventory');
            }}
          />
        ) : null}

        {tab === 'recipes' ? <RecipesScreen locations={locations} /> : null}
      </main>

      <nav className={styles.nav} aria-label="Navigation principale">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={[styles.navButton, tab === entry.id ? styles.navButtonActive : '']
              .filter(Boolean)
              .join(' ')}
            aria-current={tab === entry.id ? 'page' : undefined}
            onClick={() => {
              setTab(entry.id);
            }}
          >
            <span className={styles.navIcon} aria-hidden="true">
              {entry.icon}
            </span>
            {entry.label}
          </button>
        ))}
      </nav>
    </div>
  );
}

export function App() {
  if (configError) return <ConfigurationScreen message={configError} />;
  return (
    <CapabilitiesProvider>
      <Shell />
    </CapabilitiesProvider>
  );
}
