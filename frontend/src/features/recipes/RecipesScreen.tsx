import { useState } from 'react';
import { ApiError, describeError } from '../../api/client';
import { suggestRecipes } from '../../api/endpoints';
import type { RecipeSuggestion, StorageLocation } from '../../api/types';
import { Badge, Button, Callout, Chip, ChipRow, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useCapabilities } from '../../context/capabilitiesContext';
import { ProviderSetup } from './ProviderSetup';
import styles from './Recipes.module.css';

interface Props {
  locations: StorageLocation[];
}

function RecipeCard({ recipe }: { recipe: RecipeSuggestion }) {
  const missing = recipe.ingredients.filter((ingredient) => !ingredient.in_stock);

  return (
    <article className={styles.card} aria-labelledby={`recipe-${recipe.id}`}>
      <header className={styles.cardHeader}>
        {/* h2: the screen heading is the only h1, so levels must not skip. */}
        <h2 className={styles.title} id={`recipe-${recipe.id}`}>
          {recipe.title}
        </h2>
        {recipe.summary ? <p className={styles.summary}>{recipe.summary}</p> : null}
        <div className={styles.facts}>
          {recipe.duration_minutes !== null ? (
            <Badge tone="neutral">{recipe.duration_minutes} min</Badge>
          ) : null}
          {recipe.servings !== null ? (
            <Badge tone="neutral">
              {recipe.servings} portion{recipe.servings > 1 ? 's' : ''}
            </Badge>
          ) : null}
          {recipe.uses_expiring_soon ? <Badge tone="warn">utilise du périssable</Badge> : null}
          {missing.length === 0 ? (
            <Badge tone="ok">tout est en stock</Badge>
          ) : (
            <Badge tone="warn">
              {missing.length} ingrédient{missing.length > 1 ? 's' : ''} manquant
              {missing.length > 1 ? 's' : ''}
            </Badge>
          )}
        </div>
      </header>

      <div>
        <p className={styles.sectionTitle}>Ingrédients</p>
        <ul className={styles.ingredients}>
          {recipe.ingredients.map((ingredient, index) => (
            <li
              key={`${recipe.id}-${String(index)}-${ingredient.name}`}
              className={[styles.ingredient, ingredient.in_stock ? '' : styles.ingredientMissing]
                .filter(Boolean)
                .join(' ')}
            >
              <span>
                {ingredient.name}
                {ingredient.in_stock ? null : (
                  <>
                    {' '}
                    <Badge tone="warn">à acheter</Badge>
                  </>
                )}
              </span>
              <span className={styles.ingredientAmount}>
                {[ingredient.amount, ingredient.unit].filter(Boolean).join(' ')}
              </span>
            </li>
          ))}
        </ul>
      </div>

      <div>
        <p className={styles.sectionTitle}>Préparation</p>
        <ol className={styles.steps}>
          {recipe.steps.map((step, index) => (
            <li key={`${recipe.id}-step-${String(index)}`}>{step}</li>
          ))}
        </ol>
      </div>
    </article>
  );
}

export function RecipesScreen({ locations }: Props) {
  const { capabilities } = useCapabilities();
  const [notes, setNotes] = useState('');
  const [maxSuggestions, setMaxSuggestions] = useState(3);
  const [selectedLocations, setSelectedLocations] = useState<string[]>([]);
  const [suggestions, setSuggestions] = useState<RecipeSuggestion[] | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notConfigured, setNotConfigured] = useState(false);

  const toggleLocation = (id: string) => {
    setSelectedLocations((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id],
    );
  };

  const run = () => {
    setLoading(true);
    setError(null);
    setNotConfigured(false);

    suggestRecipes({
      location_ids: selectedLocations,
      max_suggestions: maxSuggestions,
      notes: notes.trim(),
    })
      .then((response) => {
        setSuggestions(response.suggestions);
        setModel(response.model);
      })
      .catch((cause: unknown) => {
        // 409 provider-not-configured is a configuration state, not a failure —
        // the contract requires a setup screen rather than a raw error.
        if (
          cause instanceof ApiError &&
          cause.status === 409 &&
          (cause.problemType === 'provider-not-configured' || cause.problemType === null)
        ) {
          setNotConfigured(true);
          return;
        }
        setError(describeError(cause));
      })
      .finally(() => {
        setLoading(false);
      });
  };

  const providerMissing = notConfigured || capabilities?.configured === false;

  return (
    <section className={styles.screen} aria-labelledby="recipes-heading">
      <h1 className={styles.heading} id="recipes-heading">
        Que cuisiner ?
      </h1>
      <p className={styles.lead}>
        Les propositions sont construites à partir de ce qui est réellement en stock, en
        privilégiant ce qui périme bientôt.
      </p>

      {providerMissing ? (
        <ProviderSetup capabilities={capabilities} />
      ) : (
        <>
          <div className={styles.controls}>
            <ChipRow label="Limiter à certains emplacements">
              {locations.map((location) => (
                <Chip
                  key={location.id}
                  active={selectedLocations.includes(location.id)}
                  onClick={() => {
                    toggleLocation(location.id);
                  }}
                >
                  {location.name}
                </Chip>
              ))}
            </ChipRow>

            <Field
              label="Contraintes"
              hint="Par exemple : rapide, sans four, végétarien, pour deux."
            >
              {({ id, describedBy }) => (
                <input
                  id={id}
                  aria-describedby={describedBy}
                  className={controlClass()}
                  type="text"
                  value={notes}
                  onChange={(event) => {
                    setNotes(event.target.value);
                  }}
                />
              )}
            </Field>

            <Field label="Nombre de propositions">
              {({ id, describedBy }) => (
                <select
                  id={id}
                  aria-describedby={describedBy}
                  className={controlClass()}
                  value={maxSuggestions}
                  onChange={(event) => {
                    setMaxSuggestions(Number(event.target.value));
                  }}
                >
                  {[1, 2, 3, 4, 5].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              )}
            </Field>

            <Button variant="primary" block loading={loading} onClick={run}>
              Proposer des recettes
            </Button>
          </div>

          <div aria-live="polite" aria-busy={loading}>
            {loading ? (
              <p className={styles.lead}>Recherche de recettes à partir de votre stock…</p>
            ) : error ? (
              <Callout tone="danger" title="Suggestions indisponibles">
                <p>{error}</p>
                <Button variant="secondary" onClick={run}>
                  Réessayer
                </Button>
              </Callout>
            ) : suggestions === null ? (
              <p className={styles.lead}>Aucune suggestion demandée pour l’instant.</p>
            ) : suggestions.length === 0 ? (
              <Callout tone="info" title="Aucune recette proposée">
                <p>
                  Le stock actuel n’a pas permis de composer une recette. Ajoutez quelques articles
                  ou assouplissez les contraintes.
                </p>
              </Callout>
            ) : (
              <p className={styles.lead}>
                {suggestions.length} proposition{suggestions.length > 1 ? 's' : ''}
                {model ? ` — modèle ${model}` : ''}.
              </p>
            )}
          </div>

          {suggestions && suggestions.length > 0 ? (
            <div className={styles.results}>
              {suggestions.map((recipe) => (
                <RecipeCard key={recipe.id} recipe={recipe} />
              ))}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
