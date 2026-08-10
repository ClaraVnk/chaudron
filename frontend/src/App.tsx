import { useCallback, useState } from 'react';
import { configError } from './api/config';
import { DegradedBanner } from './components/DegradedBanner';
import { CapabilitiesProvider } from './context/CapabilitiesProvider';
import { useCapabilities } from './context/capabilitiesContext';
import { SessionProvider } from './context/SessionProvider';
import { useSession } from './context/sessionContext';
import { AccountMenu } from './features/auth/AccountMenu';
import { AuthScreen } from './features/auth/AuthScreen';
import { HouseholdPicker } from './features/auth/HouseholdPicker';
import { PasswordResetScreen } from './features/auth/PasswordResetScreen';
import { takeResetToken } from './features/auth/resetLink';
import { AddItemScreen } from './features/add/AddItemScreen';
import { BudgetScreen } from './features/budget/BudgetScreen';
import { HouseholdScreen } from './features/household/HouseholdScreen';
import { JoinHouseholdPanel } from './features/household/JoinHouseholdPanel';
import { InventoryScreen } from './features/inventory/InventoryScreen';
import { RecipesScreen } from './features/recipes/RecipesScreen';
import { ShoppingScreen } from './features/shopping/ShoppingScreen';
import { useLocations } from './hooks/useLocations';
import { useMembers } from './hooks/useMembers';
import styles from './components/AppShell.module.css';

type Tab = 'inventory' | 'add' | 'recipes' | 'shopping' | 'budget' | 'household';

/**
 * Every icon here is decorative — `aria-hidden`, with the label beside it — but
 * a character no installed font can draw is worse than no icon at all: the tab
 * keeps the space and paints nothing.
 *
 * `▦` (U+25A6) and `▤` (U+25A4) were exactly that. Neither is in the Geometric
 * Shapes coverage of the fonts this application asks for, so both fell back to
 * `.notdef` and rendered blank. Checked, not eyeballed: an editor and a terminal
 * have their own font stacks and will happily show a glyph the browser cannot.
 * The check is a canvas `measureText` against a private-use codepoint no font
 * defines — same advance width means the same fallback, i.e. nothing drawn.
 *
 * The replacements are emoji, which is what the two working icons in this list
 * already were: colour emoji have their own font on every platform that matters,
 * so they cannot go missing the way a lone Geometric Shapes character can.
 */
const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: 'inventory', label: 'Inventaire', icon: '📦' },
  { id: 'add', label: 'Ajouter', icon: '＋' },
  { id: 'recipes', label: 'Recettes', icon: '🍲' },
  { id: 'shopping', label: 'Courses', icon: '🛒' },
  { id: 'budget', label: 'Budget', icon: '📊' },
  { id: 'household', label: 'Foyer', icon: '☺' },
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
      {/*
        No longer "fill in VITE_API_BASE_URL and rebuild": the API address now
        defaults to the origin the page was served from, so the ordinary
        deployment needs no build-time value at all. Reaching this screen means
        the page has no usable origin — opened from `file://`, or in a sandboxed
        frame — which rebuilding does not fix.
      */}
      <p>
        Servez cette page en <code>http://</code> ou <code>https://</code> plutôt que depuis un
        fichier local. Si l’API vit sur une autre origine que l’interface, renseignez{' '}
        <code>VITE_API_BASE_URL</code> dans <code>frontend/.env.local</code> et reconstruisez.
      </p>
    </main>
  );
}

interface ShellProps {
  tab: Tab;
  onTabChange: (tab: Tab) => void;
}

function Shell({ tab, onTabChange }: ShellProps) {
  const [inventoryVersion, setInventoryVersion] = useState(0);
  const {
    locations,
    settled: locationsSettled,
    error: locationsError,
    reload: reloadLocations,
    add: addLocation,
  } = useLocations();
  const members = useMembers();

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
        {/* Brand tagline, verbatim from assets/logo.svg — brand copy, not UI copy.
            French because every other string on this screen is: an English
            sentence in the header of a French interface reads as an oversight,
            not as branding. Change both together or they drift. */}
        <span className={styles.tagline}>Jetez-y ce que vous avez, voyez ce qui en sort.</span>
        <AccountMenu />
      </header>

      <DegradedBanner
        onConfigureProvider={() => {
          onTabChange('recipes');
        }}
      />

      <main className={styles.main} id="main" tabIndex={-1}>
        {tab === 'inventory' ? (
          <InventoryScreen
            locations={locations}
            locationsSettled={locationsSettled}
            locationsError={locationsError}
            refreshToken={inventoryVersion}
            onChanged={handleInventoryChanged}
            onLocationCreated={addLocation}
            onAddItem={() => {
              onTabChange('add');
            }}
          />
        ) : null}

        {tab === 'add' ? (
          <AddItemScreen
            locations={locations}
            locationsError={locationsError}
            onAdded={handleInventoryChanged}
            onLocationCreated={addLocation}
            onGoToInventory={() => {
              onTabChange('inventory');
            }}
          />
        ) : null}

        {tab === 'recipes' ? <RecipesScreen locations={locations} members={members} /> : null}

        {tab === 'shopping' ? <ShoppingScreen /> : null}

        {tab === 'budget' ? <BudgetScreen /> : null}

        {tab === 'household' ? <HouseholdScreen state={members} /> : null}
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
              onTabChange(entry.id);
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

/**
 * Decides which of four states the application is in.
 *
 * The current tab is held **above** this gate on purpose. A session can end
 * between two requests — expiry, an owner revoking it, the account being
 * disabled — and when it does the shell is replaced by the sign-in form. Keeping
 * the tab here means signing back in returns the user to the screen they were
 * on, instead of dropping them on the inventory with no explanation.
 */
function Gate({ tab, onTabChange }: ShellProps) {
  const { session, loading, activeHousehold, adopt, selectHousehold, signOut } = useSession();
  const [wasSignedIn, setWasSignedIn] = useState(false);
  // Read once, on the first render, and removed from the address bar by the same
  // call (`features/auth/resetLink.ts`). The lazy initialiser is what keeps it
  // from being read again on every re-render, when it is already gone.
  const [resetToken, setResetToken] = useState<string | null>(takeResetToken);

  // Before the session check, deliberately. Somebody following a reset link on a
  // device where they are still signed in has a specific intention, and it is not
  // "look at my inventory" — most likely they are resetting *because* they think
  // a session is not theirs. Completing the reset revokes every session anyway,
  // so the screen underneath would be stale within the second.
  if (resetToken !== null) {
    return (
      <PasswordResetScreen
        token={resetToken}
        onDone={() => {
          setResetToken(null);
        }}
      />
    );
  }

  if (loading) {
    // No spinner: the check is one request against a warm connection, and a
    // flash of "loading" on every load is worse than a blank frame.
    return <main className={styles.fatal} aria-busy="true" />;
  }

  if (!session) {
    return (
      <AuthScreen
        expired={wasSignedIn}
        onSignedIn={(next) => {
          setWasSignedIn(true);
          adopt(next);
        }}
      />
    );
  }

  if (!activeHousehold) {
    if (session.households.length === 0) {
      // Reachable today, and not only in theory: an owner who leaves a household
      // that still has another owner ends up here. It used to be a dead end —
      // "ask an owner to invite you" and a sign-out button — even for somebody
      // who had already been sent a code, because the panel that redeems one
      // lived inside `HouseholdScreen` and the shell never reaches that screen
      // without an active household. `JoinHouseholdPanel` is mounted here
      // instead: it is the one panel that sends no `X-Household-Id`, precisely
      // because the account is not yet a member of the household it is joining,
      // so it works with nothing selected. It re-reads the session on success,
      // which leaves exactly one household, which `SessionProvider` then adopts
      // on its own — this screen unmounts and the shell appears.
      return (
        <main className={styles.fatal}>
          <h1>Aucun foyer</h1>
          <p>
            Votre compte n’appartient à aucun foyer. Demandez au propriétaire d’un foyer de vous
            inviter, puis collez ci-dessous le code qu’il vous transmettra.
          </p>
          <JoinHouseholdPanel />
          <button type="button" onClick={() => void signOut()}>
            Se déconnecter
          </button>
        </main>
      );
    }
    return (
      <HouseholdPicker
        households={session.households}
        onSelect={selectHousehold}
        onSignOut={() => void signOut()}
      />
    );
  }

  return (
    // Keyed on the household: switching one must refetch every screen rather
    // than show the previous household's stock under the new name.
    <CapabilitiesProvider key={activeHousehold.id}>
      <ShellWhenSettled tab={tab} onTabChange={onTabChange} />
    </CapabilitiesProvider>
  );
}

/**
 * Holds the shell's first frame until the capability probe has answered.
 *
 * `DegradedBanner` sits above `main` and appears only once that probe resolves,
 * which moved everything below it down by the banner's whole height — the
 * application's largest layout shift, and one no stylesheet can reach because
 * the element simply is not in the document yet. Waiting removes it at the
 * source: nothing moves if nothing was drawn first.
 *
 * The same blank frame the session check already uses, for the same reason
 * (`Gate` above): a spinner that flashes for one round trip reads as a fault.
 * `paintable` is bounded in `CapabilitiesProvider`, so a hanging probe delays
 * this frame and never cancels it.
 */
function ShellWhenSettled({ tab, onTabChange }: ShellProps) {
  const { paintable } = useCapabilities();
  if (!paintable) return <main className={styles.fatal} aria-busy="true" />;
  return <Shell tab={tab} onTabChange={onTabChange} />;
}

export function App() {
  const [tab, setTab] = useState<Tab>('inventory');
  if (configError) return <ConfigurationScreen message={configError} />;
  return (
    <SessionProvider>
      <Gate tab={tab} onTabChange={setTab} />
    </SessionProvider>
  );
}
