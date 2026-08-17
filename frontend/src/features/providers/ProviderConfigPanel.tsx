import { useMemo, useState, type FormEvent } from 'react';
import { ApiError, describeError } from '../../api/client';
import {
  Badge,
  Button,
  Callout,
  Checkbox,
  Field,
  Fieldset,
  LoadingRow,
  Radio,
} from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { useSession } from '../../context/sessionContext';
import { useProviderConfigs } from './useProviderConfigs';
import type { ProviderChoice, ProviderConfig, ProviderMode } from './api';
import styles from './Providers.module.css';

/** Matches the server's `MIN_API_KEY_LENGTH`, so a typo is caught before a round trip. */
const KEY_MIN = 8;

/**
 * Where each provider processes what is sent to it.
 *
 * Consent is only informed if the person giving it knows **who receives the data
 * and where** (GDPR art. 7(2), art. 13(1)(f)). `docs/security-model.md` §8.3 names
 * the same split and calls it a selection criterion: Anthropic, OpenAI and Google
 * process in the United States, which is a Chapter V transfer; Mistral processes in
 * the EU; Ollama transmits to nobody when it runs on a machine the household
 * controls. The wording below is not decoration and must not be shortened into
 * "a third party".
 */
const WHERE_IT_GOES: Record<string, string> = {
  anthropic: 'Anthropic, aux États-Unis',
  openai: 'OpenAI, aux États-Unis',
  gemini: 'Google, aux États-Unis',
  mistral: 'Mistral, en France (Union européenne)',
};

const MODE_LABEL: Record<ProviderMode, string> = {
  byok: 'Ma propre clé',
  ollama: 'Mon serveur Ollama',
  instance_owner: 'La clé de l’hébergeur',
};

const STATUS_LABEL: Record<ProviderConfig['status'], string> = {
  unverified: 'Jamais utilisée',
  verified: 'Vérifiée',
  invalid_credentials: 'Clé refusée',
  disabled: 'Désactivée',
};

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('fr-CH', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
}

function recipientOf(config: { provider: string; base_url: string | null }): string {
  return WHERE_IT_GOES[config.provider] ?? config.base_url ?? 'le serveur indiqué';
}

/**
 * What leaves the instance, said in full, next to the control that authorises it.
 *
 * Its own component because it has to appear in two places — beside the consent
 * checkbox when the agreement is being given, and in the summary once it has been —
 * and a consent notice that drifts between the two is a consent notice that was not
 * really read.
 *
 * The list is what `infra/llm/prompts.py` actually builds. The allergens are
 * deliberately *absent*: `services/dietary.py` removes forbidden products from the
 * inventory before the call and re-checks the answer afterwards, so the structured
 * health fields never reach a prompt. What does reach it is the part no filter can
 * generalise, and that is what is named here.
 */
function WhatLeaves({ recipient }: { recipient: string }) {
  return (
    <ul className={styles.consentList}>
      <li>
        les produits en stock et leurs dates de péremption, pour que la recette porte sur ce que
        vous avez ;
      </li>
      <li>
        l’âge des enfants du foyer quand il y en a — une recette pour un nourrisson est dangereuse
        sans cette information ;
      </li>
      <li>
        les remarques que vous avez écrites vous-même sur l’alimentation d’une personne, telles
        quelles ;
      </li>
      <li>
        la photographie entière d’un ticket de caisse, si vous en importez une, avec tout ce qui
        figure dessus ;
      </li>
      <li>
        rien d’autre : ni votre adresse e-mail, ni le nom de votre foyer, ni vos allergies
        déclarées, qui sont appliquées ici et jamais transmises.
      </li>
      <li>
        <strong>Destinataire : {recipient}.</strong> Une fois parti, ce qui a été envoyé suit les
        conditions de ce tiers et non celles de Chaudron.
      </li>
    </ul>
  );
}

/**
 * The one sentence `docs/security-model.md` §P3 requires, where it requires it.
 *
 * Against a malicious or negligent instance operator **there is no technical
 * control**: the server has to decrypt the key in order to call the provider, so
 * whoever runs the server can read it. That is a property of the BYOK model, not a
 * defect to be fixed later, and the threat model's design consequence is explicit —
 * the fact must be written into the interface *at the moment the user pastes their
 * key*, not filed in a help page nobody opens.
 *
 * "An informed user who accepts is not a victim; a user who is unaware is one."
 * Rendered as a warning callout directly above the field rather than as a hint
 * underneath it, because the point is to be read before the paste and not after.
 */
function OperatorCanReadThisKey() {
  return (
    <Callout tone="warn" title="Qui peut lire cette clé">
      <p>
        La personne qui administre cette instance peut techniquement lire cette clé : le serveur
        doit la déchiffrer pour appeler le fournisseur, il n’existe aucune protection contre cela.
        Utilisez une clé dédiée à Chaudron, avec un plafond de dépense chez votre fournisseur, et
        pas la clé que vous employez ailleurs.
      </p>
    </Callout>
  );
}

/**
 * Configuring the model provider of a household: the screen ADR-0007 always
 * assumed and that did not exist.
 *
 * Four rules shape it, and each one is a rule the server also enforces — the
 * interface states them, it does not implement them.
 *
 * **The agreement is a separate gesture from the key.** Pasting a credential says
 * "I have an account there"; ticking the box says "send what my household eats, and
 * who eats it, to this company". The box starts unticked, is never ticked on the
 * user's behalf, and sits under a plain-language list of exactly what will leave —
 * which is the informing half of art. 7 and the half a checkbox alone does not
 * deliver.
 *
 * **The key is never shown again.** The server returns it in no form; the last four
 * characters come back so someone with two accounts can tell which key is
 * installed. Changing it means pasting a new one, which is also how it is rotated.
 *
 * **Withdrawing keeps the record.** The configuration survives, so this screen keeps
 * showing what was authorised and when it stopped. It takes effect at the next
 * request, not at some later cleanup.
 *
 * **A household has one configuration, or none.** So this is "your configuration"
 * and not a list: no card competes with another, and the form appears only when
 * there is nothing configured. Changing provider is retire-then-register, in that
 * order, because the server refuses a second one rather than overwriting a working
 * API key — and the button that retires says so before it is pressed.
 *
 * **A local Ollama needs no agreement, and "local" is not the same as "Ollama".**
 * The server decides that from the address it would dial, so this screen offers the
 * agreement for every Ollama and explains that it is only required when the server
 * is somebody else's. Claiming otherwise here would be the interface making a
 * promise the server does not keep.
 */
export function ProviderConfigPanel() {
  const state = useProviderConfigs();
  const { activeHousehold } = useSession();
  // Creating accepts a credential and consents in the household's name; every
  // other write here rotates, retires or withdraws. The server refuses all of them
  // to anyone but an owner, and this hides the form rather than letting a member
  // fill it in and meet a 403 at the end. The check is a courtesy — the refusal
  // that matters is the server's.
  const isOwner = activeHousehold?.role === 'owner';

  const [adding, setAdding] = useState(false);
  const [status, setStatus] = useState('');

  // At most one, and that is a database rule rather than an expectation
  // (`uq_llm_provider_config_household_active`). The API still publishes an array,
  // so the extra rows a future relaxation might bring would simply not be drawn —
  // which is a better failure than a screen that renders half a decision.
  const config = state.configs[0] ?? null;

  if (state.unsupported) {
    return (
      <Callout tone="info" title="Cette instance ne propose pas encore cet écran">
        <p>
          Le serveur auquel cette application est reliée est antérieur à la configuration des
          fournisseurs de modèle. Les suggestions de recettes se règlent alors côté serveur.
        </p>
      </Callout>
    );
  }

  return (
    <section className={styles.panel} aria-labelledby="provider-config-heading">
      <div className={styles.head}>
        <h2 className={styles.heading} id="provider-config-heading">
          Fournisseur de modèle
        </h2>
        {config !== null ? (
          <Badge tone={config.is_permitted ? 'ok' : 'warn'}>
            {config.is_permitted ? 'Actif' : 'En attente'}
          </Badge>
        ) : null}
      </div>

      <p className={styles.lead}>
        Les suggestions de recettes et la lecture d’un ticket photographié ont besoin d’un modèle.
        Tout le reste de Chaudron — l’inventaire, le scan, la liste de courses — fonctionne sans, et
        tant que rien n’est enregistré ici, aucune donnée de ce foyer ne quitte cette instance.
      </p>

      <p className="visually-hidden" role="status">
        {status}
      </p>

      {state.loading ? <LoadingRow label="Chargement de la configuration…" /> : null}

      {state.error !== null ? (
        <Callout tone="warn" title="Impossible de lire la configuration">
          <p>{state.error}</p>
          <div className={styles.actions}>
            <Button variant="secondary" onClick={state.reload}>
              Réessayer
            </Button>
          </div>
        </Callout>
      ) : null}

      {!state.loading && config === null && !adding ? (
        <Callout tone="info" title="Aucun fournisseur enregistré">
          <p>
            {isOwner
              ? 'Choisissez comment ce foyer accède à un modèle : votre propre clé chez un fournisseur, ou un serveur Ollama que vous hébergez et qui n’envoie rien à personne.'
              : 'Personne n’a encore configuré de fournisseur pour ce foyer. Seul un propriétaire du foyer peut le faire.'}
          </p>
        </Callout>
      ) : null}

      {config !== null ? (
        <ConfiguredProvider config={config} isOwner={isOwner} state={state} onStatus={setStatus} />
      ) : null}

      {/* No "add another": the server answers 409 to a second one, and a button
          that exists in order to be refused is a button that teaches people the
          screen is broken. The way to change provider is the "Retirer ce
          fournisseur" action on the card above, after which this form returns. */}
      {isOwner && !state.loading && config === null ? (
        adding ? (
          <NewProviderForm
            catalogue={state.catalogue}
            onCancel={() => {
              setAdding(false);
            }}
            onCreate={async (draft) => {
              await state.create(draft);
              setStatus('Le fournisseur est enregistré.');
              setAdding(false);
            }}
          />
        ) : (
          <div className={styles.actions}>
            <Button
              variant="primary"
              onClick={() => {
                setAdding(true);
              }}
            >
              Configurer un fournisseur
            </Button>
          </div>
        )
      ) : null}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* One configured provider                                                     */
/* -------------------------------------------------------------------------- */

function ConfiguredProvider({
  config,
  isOwner,
  state,
  onStatus,
}: {
  config: ProviderConfig;
  isOwner: boolean;
  state: ReturnType<typeof useProviderConfigs>;
  onStatus: (message: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rotating, setRotating] = useState(false);
  const [newKey, setNewKey] = useState('');

  const run = (name: string, work: () => Promise<void>, done: string) => {
    setBusy(name);
    setError(null);
    work()
      .then(() => {
        onStatus(done);
      })
      .catch((cause: unknown) => {
        setError(describeError(cause));
      })
      .finally(() => {
        setBusy(null);
      });
  };

  return (
    <div className={styles.card}>
      <div className={styles.cardHead}>
        <span className={styles.cardName}>{config.label}</span>
        <Badge tone={config.is_permitted ? 'ok' : 'warn'}>
          {config.is_permitted ? 'Utilisable' : 'Accord retiré'}
        </Badge>
      </div>

      <p className={styles.cardLine}>
        <span className={styles.cardLabel}>Accès</span>
        {MODE_LABEL[config.mode]} — {config.provider}, modèle <code>{config.model}</code>
      </p>
      {config.base_url !== null ? (
        <p className={styles.cardLine}>
          <span className={styles.cardLabel}>Adresse du serveur</span>
          <code>{config.base_url}</code>
        </p>
      ) : null}
      {config.api_key_last4 !== null ? (
        <p className={styles.cardLine}>
          <span className={styles.cardLabel}>Clé enregistrée</span>
          <span>
            se terminant par <code>{config.api_key_last4}</code>
            {config.api_key_set_at !== null
              ? ` (depuis le ${formatDate(config.api_key_set_at)})`
              : ''}
          </span>
        </p>
      ) : null}
      <p className={styles.cardLine}>
        <span className={styles.cardLabel}>Ce que le modèle sait faire</span>
        <span>
          {config.capabilities.vision ? 'lit les images' : 'ne lit pas les images'}
          {' · '}
          {config.capabilities.structured_output
            ? 'réponses structurées'
            : 'pas de réponse structurée garantie'}
          {config.max_context_tokens !== null
            ? ` · ${config.max_context_tokens.toLocaleString('fr-CH')} jetons de contexte`
            : ''}
        </span>
      </p>
      <p className={styles.cardLine}>
        <span className={styles.cardLabel}>État</span>
        {STATUS_LABEL[config.status]}
        {config.last_error !== null ? ` — ${config.last_error}` : ''}
      </p>

      {config.consent_required ? (
        <>
          <p className={styles.cardLine}>
            <span className={styles.cardLabel}>Accord donné le</span>
            {config.consented_at !== null ? formatDate(config.consented_at) : 'jamais'}
          </p>
          {config.consent_revoked_at !== null ? (
            <p className={styles.cardLine}>
              <span className={styles.cardLabel}>Retiré le</span>
              {formatDate(config.consent_revoked_at)}
            </p>
          ) : null}
          {config.is_consented ? (
            <details className={styles.consentDetails}>
              <summary>Ce qui part vers {recipientOf(config)}</summary>
              <WhatLeaves recipient={recipientOf(config)} />
            </details>
          ) : (
            <Callout tone="info" title="Envoi désactivé">
              <p>
                L’accord a été retiré : Chaudron n’envoie plus rien à ce fournisseur, à partir de la
                requête suivante. La configuration est conservée pour que vous puissiez voir ce qui
                avait été autorisé, et quand cela a cessé.
              </p>
            </Callout>
          )}
        </>
      ) : (
        <Callout tone="info" title="Aucun accord nécessaire">
          <p>
            Ce serveur est joignable sans sortir de votre réseau : rien n’est transmis à un tiers,
            il n’y a donc rien à autoriser. Si vous déplacez ce serveur sur Internet, Chaudron vous
            demandera votre accord avant de continuer à l’utiliser.
          </p>
        </Callout>
      )}

      {error !== null ? (
        <Callout tone="danger" title="Opération impossible">
          <p>{error}</p>
        </Callout>
      ) : null}

      {/*
        Un membre non propriétaire voyait cette carte sans un seul bouton et
        sans un mot d'explication : la phrase « seul un propriétaire peut le
        faire » n'existait que dans le cas où AUCUNE configuration n'est
        enregistrée. Demandé le 2026-08-17 par quelqu'un qui cherchait où
        changer sa clé et concluait que l'option n'existait pas.

        Une absence de commande sans raison se lit comme un défaut du logiciel,
        pas comme une permission manquante — et c'est la lecture la plus coûteuse
        des deux, parce qu'elle envoie chercher ailleurs.
      */}
      {!isOwner ? (
        <Callout tone="info" title="Lecture seule">
          <p>
            Vous êtes membre de ce foyer, pas propriétaire : vous voyez comment il accède au modèle,
            mais seuls les propriétaires peuvent remplacer la clé, retirer la configuration ou
            modifier l’accord. Demandez à un propriétaire du foyer.
          </p>
        </Callout>
      ) : null}

      {isOwner ? (
        <>
          {rotating ? (
            <form
              className={styles.form}
              onSubmit={(event) => {
                event.preventDefault();
                const cleaned = newKey.trim();
                if (cleaned.length < KEY_MIN) {
                  setError('Collez la clé en entier.');
                  return;
                }
                run(
                  'rotate',
                  async () => {
                    await state.update(config.id, { api_key: cleaned });
                    setNewKey('');
                    setRotating(false);
                  },
                  'La clé a été remplacée.',
                );
              }}
              noValidate
            >
              <OperatorCanReadThisKey />
              <Field
                label="Nouvelle clé d’API"
                required
                hint="La clé est chiffrée sur le serveur et ne vous est jamais réaffichée : seuls ses quatre derniers caractères restent visibles. Enregistrer une nouvelle clé remplace l’ancienne."
              >
                {({ id, describedBy, invalid }) => (
                  <input
                    id={id}
                    className={controlClass(invalid)}
                    // `password`, so the browser masks it, keeps it out of the
                    // autofill history of ordinary text fields, and does not offer
                    // it back on the next form.
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    value={newKey}
                    aria-describedby={describedBy}
                    onChange={(event) => {
                      setNewKey(event.target.value);
                    }}
                  />
                )}
              </Field>
              <div className={styles.actions}>
                <Button type="submit" variant="primary" loading={busy === 'rotate'}>
                  Remplacer la clé
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => {
                    setNewKey('');
                    setRotating(false);
                    setError(null);
                  }}
                >
                  Annuler
                </Button>
              </div>
            </form>
          ) : (
            <div className={styles.actions}>
              {config.mode === 'byok' ? (
                <Button
                  variant="secondary"
                  onClick={() => {
                    setRotating(true);
                  }}
                >
                  Changer la clé
                </Button>
              ) : null}
              {config.mode === 'ollama' ? (
                <Button
                  variant="secondary"
                  loading={busy === 'probe'}
                  onClick={() => {
                    run(
                      'probe',
                      () => state.probe(config.id),
                      'Les capacités du serveur ont été redétectées.',
                    );
                  }}
                >
                  Redétecter les capacités
                </Button>
              ) : null}
              {config.consent_required && !config.is_consented ? (
                <Button
                  variant="primary"
                  loading={busy === 'grant'}
                  onClick={() => {
                    run('grant', () => state.grantConsent(config.id), 'L’accord a été redonné.');
                  }}
                >
                  Redonner l’accord
                </Button>
              ) : null}
              {config.consent_required && config.is_consented ? (
                <Button
                  variant="danger"
                  loading={busy === 'withdraw'}
                  onClick={() => {
                    run(
                      'withdraw',
                      () => state.withdrawConsent(config.id),
                      'L’accord a été retiré. Plus rien n’est envoyé à ce fournisseur.',
                    );
                  }}
                >
                  Retirer l’accord
                </Button>
              ) : null}
              <Button
                variant="ghost"
                loading={busy === 'archive'}
                onClick={() => {
                  run(
                    'archive',
                    () => state.archive(config.id),
                    'Le fournisseur a été retiré. Vous pouvez en enregistrer un autre.',
                  );
                }}
              >
                Retirer ce fournisseur
              </Button>
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* The form                                                                    */
/* -------------------------------------------------------------------------- */

interface Draft {
  label: string;
  mode: ProviderMode;
  provider: string;
  model: string;
  base_url?: string;
  api_key?: string;
  consent_granted: boolean;
}

/** Which providers each mode can serve, mirroring `_check_coherence` on the server. */
function providersFor(catalogue: ProviderChoice[], mode: ProviderMode): ProviderChoice[] {
  if (mode === 'ollama') return catalogue.filter((choice) => choice.code === 'ollama');
  return catalogue.filter((choice) => choice.code !== 'ollama');
}

function NewProviderForm({
  catalogue,
  onCreate,
  onCancel,
}: {
  catalogue: ProviderChoice[];
  onCreate: (draft: Draft) => Promise<void>;
  onCancel: () => void;
}) {
  const [mode, setMode] = useState<ProviderMode>('byok');
  const [label, setLabel] = useState('');
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [consent, setConsent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const available = useMemo(() => providersFor(catalogue, mode), [catalogue, mode]);
  const chosen = available.find((choice) => choice.code === provider) ?? available[0];
  const effectiveProvider = chosen?.code ?? '';
  const models = chosen?.models ?? [];
  const effectiveModel = model || (chosen?.default_model ?? '');

  // Ollama is the one mode whose need for an agreement the browser cannot decide:
  // the server settles it from the address it would dial, and a container name
  // says nothing about where it points. So the box is offered and explained rather
  // than hidden, and the server's refusal is what makes it required.
  const consentMandatory = mode !== 'ollama';

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    const draft: Draft = {
      label: label.trim(),
      mode,
      provider: effectiveProvider,
      model: effectiveModel.trim(),
      consent_granted: consent,
    };
    if (mode === 'ollama') draft.base_url = baseUrl.trim();
    if (mode === 'byok') draft.api_key = apiKey.trim();

    if (draft.label === '') {
      setError('Donnez un nom à cette configuration, pour la reconnaître plus tard.');
      return;
    }
    if (draft.model === '') {
      setError('Choisissez un modèle.');
      return;
    }
    if (mode === 'ollama' && draft.base_url === '') {
      setError('Indiquez l’adresse de votre serveur Ollama.');
      return;
    }
    if (mode === 'byok' && (draft.api_key ?? '').length < KEY_MIN) {
      setError('Collez votre clé d’API en entier.');
      return;
    }

    setSaving(true);
    onCreate(draft)
      .catch((cause: unknown) => {
        // The server's sentences are English and describe an API; these are the
        // refusals a person can act on, so they are said in French.
        if (cause instanceof ApiError && cause.problemType === 'provider-consent-required') {
          setError(
            mode === 'ollama'
              ? 'Ce serveur Ollama n’est pas sur votre réseau : ce qui lui est envoyé part chez un tiers. Cochez l’autorisation pour continuer.'
              : 'Cochez l’autorisation : sans elle, rien n’est enregistré.',
          );
          return;
        }
        if (cause instanceof ApiError && cause.problemType === 'provider-probe-failed') {
          setError(
            'Votre serveur Ollama n’a pas répondu. Vérifiez l’adresse, et que le modèle demandé y est bien installé.',
          );
          return;
        }
        if (cause instanceof ApiError && cause.problemType === 'provider-label-taken') {
          setError('Ce foyer a déjà une configuration portant ce nom.');
          return;
        }
        // Reachable when a second owner registered one while this form was open:
        // the screen hides the button once a configuration exists, so this is the
        // race and not the ordinary path. It says what to do next rather than
        // leaving a filled-in form with an English sentence under it.
        if (cause instanceof ApiError && cause.problemType === 'provider-config-already-exists') {
          setError(
            'Ce foyer a déjà un fournisseur configuré, et il ne peut y en avoir qu’un. Retirez celui qui est enregistré avant d’en ajouter un autre, ou modifiez-le.',
          );
          return;
        }
        setError(describeError(cause));
      })
      .finally(() => {
        setSaving(false);
      });
  };

  const recipient = WHERE_IT_GOES[effectiveProvider] ?? (baseUrl.trim() || 'le serveur indiqué');

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <Fieldset
        legend="Comment ce foyer accède à un modèle"
        hint="Trois façons, et elles n’ont pas les mêmes conséquences : qui paie, et qui reçoit vos données."
      >
        <Radio
          name="provider-mode"
          value="byok"
          checked={mode === 'byok'}
          onSelect={(value) => {
            setMode(value as ProviderMode);
            setProvider('');
            setModel('');
          }}
          detail="Vous ouvrez un compte chez un fournisseur et collez votre clé. C’est vous qui payez, à l’usage."
        >
          {MODE_LABEL.byok}
        </Radio>
        <Radio
          name="provider-mode"
          value="ollama"
          checked={mode === 'ollama'}
          onSelect={(value) => {
            setMode(value as ProviderMode);
            setProvider('');
            setModel('');
          }}
          detail="Le modèle tourne sur une machine que vous hébergez. Rien n’est envoyé à personne, et rien n’est facturé."
        >
          {MODE_LABEL.ollama}
        </Radio>
        <Radio
          name="provider-mode"
          value="instance_owner"
          checked={mode === 'instance_owner'}
          onSelect={(value) => {
            setMode(value as ProviderMode);
            setProvider('');
            setModel('');
          }}
          detail="Réservé au foyer qui héberge cette instance : c’est sa facture. Les autres foyers reçoivent un refus."
        >
          {MODE_LABEL.instance_owner}
        </Radio>
      </Fieldset>

      <Field
        label="Nom de cette configuration"
        required
        hint="« Ma clé Anthropic », « Le NAS du salon »."
      >
        {({ id, describedBy, invalid }) => (
          <input
            id={id}
            className={controlClass(invalid)}
            type="text"
            autoComplete="off"
            maxLength={80}
            value={label}
            aria-describedby={describedBy}
            onChange={(event) => {
              setLabel(event.target.value);
            }}
          />
        )}
      </Field>

      {mode !== 'ollama' ? (
        <Field label="Fournisseur" required>
          {({ id, describedBy }) => (
            <select
              id={id}
              className={controlClass()}
              value={effectiveProvider}
              aria-describedby={describedBy}
              onChange={(event) => {
                setProvider(event.target.value);
                setModel('');
              }}
            >
              {available.map((choice) => (
                <option key={choice.code} value={choice.code}>
                  {choice.display_name}
                </option>
              ))}
            </select>
          )}
        </Field>
      ) : null}

      {models.length > 0 ? (
        <Field
          label="Modèle"
          required
          hint="Seuls les modèles que cette instance sait décrire sont proposés : elle doit pouvoir dire à l’avance si le modèle lit les images, sinon l’import d’un ticket photographié échouerait au moment du clic."
        >
          {({ id, describedBy }) => (
            <select
              id={id}
              className={controlClass()}
              value={effectiveModel}
              aria-describedby={describedBy}
              onChange={(event) => {
                setModel(event.target.value);
              }}
            >
              {models.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
        </Field>
      ) : (
        <Field
          label="Modèle installé sur votre serveur"
          required
          hint="Le nom exact tel qu’il apparaît dans « ollama list », par exemple llama3.2-vision. Chaudron interroge le serveur à l’enregistrement pour savoir ce que ce modèle sait faire."
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={effectiveModel}
              aria-describedby={describedBy}
              onChange={(event) => {
                setModel(event.target.value);
              }}
            />
          )}
        </Field>
      )}

      {mode === 'ollama' ? (
        <Field
          label="Adresse de votre serveur Ollama"
          required
          hint="Par exemple http://ollama:11434. L’hébergeur de cette instance doit avoir autorisé cette adresse ; sinon elle est refusée à l’enregistrement plutôt qu’au premier usage."
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="url"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              value={baseUrl}
              aria-describedby={describedBy}
              onChange={(event) => {
                setBaseUrl(event.target.value);
              }}
            />
          )}
        </Field>
      ) : null}

      {mode === 'byok' ? <OperatorCanReadThisKey /> : null}

      {mode === 'byok' ? (
        <Field
          label="Votre clé d’API"
          required
          hint="Elle est chiffrée sur le serveur et ne vous est jamais réaffichée : seuls ses quatre derniers caractères restent visibles. Pour en changer, vous en collerez une nouvelle."
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              className={controlClass(invalid)}
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={apiKey}
              aria-describedby={describedBy}
              onChange={(event) => {
                setApiKey(event.target.value);
              }}
            />
          )}
        </Field>
      ) : null}

      <div className={styles.consentBlock}>
        <p className={styles.cardLabel}>
          {mode === 'ollama'
            ? 'Ce qui partirait, si ce serveur n’est pas le vôtre'
            : 'Ce qui partira, et à qui'}
        </p>
        <WhatLeaves recipient={recipient} />
        <p className={styles.consentNote}>
          {mode === 'ollama'
            ? 'Si votre serveur Ollama est sur cette machine ou sur votre réseau local, rien ne sort et aucun accord n’est nécessaire : Chaudron le vérifie à partir de l’adresse indiquée. S’il est hébergé ailleurs, c’est un tiers qui reçoit ces données, et votre accord est alors requis.'
            : 'Vous pouvez retirer cet accord à tout moment depuis cet écran ; l’envoi cesse dès la requête suivante, et la configuration est conservée pour que vous puissiez voir ce qui avait été autorisé.'}
        </p>
        <Checkbox checked={consent} onChange={setConsent}>
          J’autorise Chaudron à envoyer les données ci-dessus à {recipient}.
        </Checkbox>
      </div>

      {error !== null ? (
        <Callout tone="danger" title="Enregistrement impossible">
          <p>{error}</p>
        </Callout>
      ) : null}

      <div className={styles.actions}>
        <Button
          type="submit"
          variant="primary"
          loading={saving}
          disabled={consentMandatory && !consent}
        >
          Enregistrer
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Annuler
        </Button>
      </div>
    </form>
  );
}
