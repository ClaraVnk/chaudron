import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Callout, Field } from '../../components/ui';
import { controlClass } from '../../components/controlClass';
import { hasValidGtinChecksum, isPlausibleGtin, normaliseGtin } from '../../lib/gtin';
import styles from './Add.module.css';

/**
 * Barcode scanning, camera side.
 *
 * Decoding uses the `barcode-detector` ponyfill over zxing-wasm, with no
 * "native BarcodeDetector if available" branch — see
 * docs/technical-notes-scanning.md §1.3. The native API exists on neither iOS,
 * Firefox, nor Chrome on Linux, so that branch would never run in local
 * development, and an untested branch is a broken branch.
 *
 * The ~1 MB WASM module is imported lazily, on the first camera start, so it
 * never costs anything on app boot.
 */

const RETAIL_FORMATS = [
  'ean_13',
  'ean_8',
  'upc_a',
  'upc_e',
  'databar',
  'databar_expanded',
] as const;

/** ~10 fps. Faster buys nothing and heats the phone. */
const DECODE_INTERVAL_MS = 100;
/** Ignore a repeat of the same code for this long (§4.7). */
const DUPLICATE_WINDOW_MS = 2000;
/** After this long without a read, stop insisting and offer manual entry (§4.2). */
const STRUGGLE_HINT_MS = 10_000;

type Phase = 'idle' | 'starting' | 'running' | 'error';

interface Props {
  onDetected: (gtin: string) => void;
  onManualEntry: () => void;
  onCancel: () => void;
}

function describeCameraError(error: unknown): string {
  if (!(error instanceof DOMException)) {
    return "La caméra n'a pas pu démarrer. Utilisez la saisie manuelle.";
  }
  switch (error.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return "Accès à la caméra refusé. Autorisez-le dans les réglages du navigateur (sur iPhone : Réglages > Safari > Caméra), puis réessayez. Vous pouvez aussi saisir l'article à la main.";
    case 'NotFoundError':
    case 'OverconstrainedError':
      return "Aucune caméra utilisable n'a été trouvée sur cet appareil. Passez par la saisie manuelle.";
    case 'NotReadableError':
      return 'La caméra est déjà utilisée par une autre application. Fermez-la puis réessayez.';
    default:
      return `La caméra n'a pas pu démarrer (${error.name}). Utilisez la saisie manuelle.`;
  }
}

/**
 * The camera stream, deliberately outside React.
 *
 * Scanning a shopping trip means twenty products in a row, and the add flow
 * unmounts this component after every single one: detect → look up → fill the
 * form → save → scan again. Each remount used to call `getUserMedia` afresh, so
 * every product paid a cold start — the permission indicator, autofocus,
 * exposure — and the user paid it twenty times. That is the complaint this
 * exists to answer.
 *
 * Holding the stream at module scope lets the next mount reattach the *same*
 * live tracks to the new `<video>`, which is instant.
 *
 * The obvious risk is the one that matters: a camera left running is a camera
 * left running, and this application lives in a kitchen. So the stream is never
 * simply kept — releasing it is *deferred*, and only for as long as somebody
 * plausibly stayed in the flow. Leaving deliberately (cancel, manual entry)
 * releases it at once, and a mount that never comes releases it on the timer.
 */
let sharedStream: MediaStream | null = null;
let releaseTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * How long the stream may outlive an unmount.
 *
 * It bounds "the camera is on while nothing is scanning", so it is a privacy
 * number rather than a performance one. Long enough to type a quantity and pick
 * a location without paying a restart; short enough that a user who wandered off
 * mid-form does not leave the LED on for the rest of the afternoon.
 */
const RELEASE_GRACE_MS = 90_000;

function isLive(stream: MediaStream | null): stream is MediaStream {
  return stream !== null && stream.getVideoTracks().some((track) => track.readyState === 'live');
}

function cancelPendingRelease(): void {
  if (releaseTimer !== null) {
    clearTimeout(releaseTimer);
    releaseTimer = null;
  }
}

/** Stop the tracks for real. The LED goes out here and nowhere else. */
function releaseSharedStream(): void {
  cancelPendingRelease();
  sharedStream?.getTracks().forEach((track) => {
    track.stop();
  });
  sharedStream = null;
}

function scheduleRelease(): void {
  cancelPendingRelease();
  releaseTimer = setTimeout(releaseSharedStream, RELEASE_GRACE_MS);
}

export function ScannerView({ onDetected, onManualEntry, onCancel }: Props) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const runningRef = useRef(false);
  const lastValueRef = useRef<{ value: string; at: number } | null>(null);
  const previousReadRef = useRef<string | null>(null);

  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [torchAvailable, setTorchAvailable] = useState(false);
  const [torchOn, setTorchOn] = useState(false);
  const [struggling, setStruggling] = useState(false);
  const [gtinInput, setGtinInput] = useState('');
  const [gtinError, setGtinError] = useState<string | null>(null);

  /**
   * Stop decoding and let go of the video element, but keep the tracks alive so
   * the next scan reattaches instead of restarting. Use this on the paths that
   * lead *back here* — a detection, which the add flow follows with a form and
   * then another scan.
   */
  const pause = useCallback(() => {
    runningRef.current = false;
    if (videoRef.current) videoRef.current.srcObject = null;
    streamRef.current = null;
  }, []);

  /** Stop decoding and end the stream. For the paths that leave the flow. */
  const release = useCallback(() => {
    pause();
    releaseSharedStream();
  }, [pause]);

  // On unmount the stream is kept and its release deferred: the add flow
  // unmounts this component between every two scans, and tearing the camera down
  // there is exactly the cold start being removed. A mount that never comes back
  // is caught by the timer instead.
  useEffect(
    () => () => {
      runningRef.current = false;
      if (videoRef.current) videoRef.current.srcObject = null;
      streamRef.current = null;
      scheduleRelease();
    },
    [],
  );

  const start = useCallback(async () => {
    setError(null);
    setStruggling(false);
    setPhase('starting');

    if (!navigator.mediaDevices?.getUserMedia) {
      setPhase('error');
      setError(
        "La caméra n'est accessible qu'en HTTPS (ou sur localhost). Ouvrez l'application via une adresse sécurisée, ou saisissez l'article à la main.",
      );
      return;
    }

    // Only the acquisition is short-circuited. Everything below — attaching the
    // element, playing, reading the track for torch support, the decode loop —
    // runs identically whether the stream is new or reused, so the fast path
    // cannot drift away from the slow one.
    let stream: MediaStream;
    if (isLive(sharedStream)) {
      cancelPendingRelease();
      stream = sharedStream;
    } else {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          // `ideal`, never `exact`: with `exact` the call simply fails on devices
          // without a rear camera, which breaks desktop development.
          video: {
            facingMode: { ideal: 'environment' },
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
          audio: false,
        });
      } catch (cause) {
        setPhase('error');
        setError(describeCameraError(cause));
        return;
      }
      sharedStream = stream;
    }

    streamRef.current = stream;
    const video = videoRef.current;
    if (!video) {
      // No element to attach to: end the stream rather than leaving it running
      // with nothing displaying it. `releaseSharedStream` rather than stopping
      // the tracks directly, so the module-level handle is cleared too and the
      // next mount does not reuse a dead stream.
      releaseSharedStream();
      return;
    }
    video.srcObject = stream;
    try {
      await video.play();
    } catch {
      // Autoplay rejection is not fatal: the element is muted and inline, and
      // decoding reads frames regardless.
    }

    const [track] = stream.getVideoTracks();
    if (track) {
      const capabilities = track.getCapabilities();
      // Absent, not disabled: iOS never exposes torch, and a permanently greyed
      // button is worse than no button.
      setTorchAvailable(capabilities.torch === true);
    }

    let detector: { detect: (source: HTMLVideoElement) => Promise<{ rawValue: string }[]> };
    try {
      const [{ BarcodeDetector, prepareZXingModule }, wasmModule] = await Promise.all([
        import('barcode-detector/ponyfill'),
        import('zxing-wasm/reader/zxing_reader.wasm?url'),
      ]);
      // Without this override zxing-wasm fetches its WASM from a CDN, which
      // breaks offline scanning and leaks a request to a third party.
      prepareZXingModule({
        overrides: { locateFile: () => wasmModule.default },
        fireImmediately: false,
      });
      detector = new BarcodeDetector({ formats: [...RETAIL_FORMATS] });
    } catch (cause) {
      release();
      setPhase('error');
      setError(
        `Le décodeur de code-barres n'a pas pu être chargé (${cause instanceof Error ? cause.message : 'erreur inconnue'}). Utilisez la saisie manuelle.`,
      );
      return;
    }

    setPhase('running');
    runningRef.current = true;
    previousReadRef.current = null;

    const struggleTimer = setTimeout(() => {
      if (runningRef.current) setStruggling(true);
    }, STRUGGLE_HINT_MS);

    const tick = async () => {
      if (!runningRef.current) return;
      const element = videoRef.current;
      if (element && element.readyState >= 2) {
        try {
          const results = await detector.detect(element);
          const raw = results[0]?.rawValue;
          if (raw) {
            const value = normaliseGtin(raw);
            // Two identical consecutive reads before accepting: cheap, and it
            // removes almost every false positive (§4.8).
            if (previousReadRef.current === value) {
              const last = lastValueRef.current;
              const now = Date.now();
              if (!last || last.value !== value || now - last.at > DUPLICATE_WINDOW_MS) {
                lastValueRef.current = { value, at: now };
                clearTimeout(struggleTimer);
                // `pause`, not `release`: the add flow comes straight back here
                // for the next product, and that is the restart being avoided.
                pause();
                navigator.vibrate?.(40);
                onDetected(value);
                return;
              }
            }
            previousReadRef.current = value;
          }
        } catch {
          // A frame that fails to decode is the normal case, not an error.
        }
      }
      if (runningRef.current) setTimeout(() => void tick(), DECODE_INTERVAL_MS);
    };

    void tick();
  }, [onDetected, pause, release]);

  /**
   * Resume automatically when the camera is *already* on, and never otherwise.
   *
   * The gate below — "elle ne démarre que sur votre demande" — exists so that
   * opening this screen never opens the camera. That property is kept exactly:
   * this effect starts nothing unless `sharedStream` is already live, which can
   * only be true because the household activated it themselves moments ago and
   * the add flow unmounted this component between two products.
   *
   * Without it, keeping the stream alive fixed the wrong half of the problem.
   * The cold start was gone — no permission prompt, no autofocus — but the tap
   * on "Activer la caméra" was still there, once per product, which is what
   * "il faut à chaque fois redémarrer la caméra" actually describes. Twenty
   * items is twenty taps on a button whose stated purpose is consent that was
   * already given and has not been withdrawn.
   *
   * A cold start still requires the tap. Cancelling, or letting the 90-second
   * timer run out, ends the stream and puts the gate back.
   */
  useEffect(() => {
    if (phase !== 'idle' || !isLive(sharedStream)) return;
    // `react-hooks/set-state-in-effect` is disabled here rather than worked
    // around, because this is the case effects exist for: a `MediaStream` is
    // state living outside React, and reattaching it is synchronising with it.
    // The alternatives are worse — deriving the phase at `useState` time cannot
    // attach anything to a `<video>` that does not exist yet, and doing it in a
    // click handler is the tap being removed.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void start();
  }, [phase, start]);

  const toggleTorch = useCallback(() => {
    const track = streamRef.current?.getVideoTracks()[0];
    if (!track) return;
    const next = !torchOn;
    void track
      .applyConstraints({ advanced: [{ torch: next }] })
      .then(() => {
        setTorchOn(next);
      })
      .catch(() => {
        setTorchAvailable(false);
      });
  }, [torchOn]);

  const submitGtin = () => {
    const digits = normaliseGtin(gtinInput);
    if (!isPlausibleGtin(digits)) {
      setGtinError('Un code-barres compte 8, 12, 13 ou 14 chiffres.');
      return;
    }
    // Validate the check digit locally: it catches a typo immediately instead of
    // producing a misleading "produit introuvable" from the server.
    if (!hasValidGtinChecksum(digits)) {
      setGtinError('Clé de contrôle invalide — vérifiez les chiffres saisis.');
      return;
    }
    setGtinError(null);
    // Typing a code by hand means the camera was not the way in; do not hold it.
    release();
    onDetected(digits);
  };

  return (
    <div className={styles.screen}>
      <h1 className={styles.heading}>Scanner un code-barres</h1>

      <div className={styles.viewport}>
        <video
          ref={videoRef}
          className={styles.video}
          playsInline
          muted
          autoPlay
          aria-label="Aperçu de la caméra"
        />
        {phase === 'running' ? <div className={styles.reticle} aria-hidden="true" /> : null}
        {phase !== 'running' ? (
          <div className={styles.viewportPlaceholder}>
            {phase === 'starting' ? (
              <p>Démarrage de la caméra…</p>
            ) : (
              <>
                <p>
                  La caméra n’est pas encore active. Elle ne démarre que sur votre demande, et
                  l’image ne quitte jamais l’appareil.
                </p>
                <Button
                  variant="primary"
                  onClick={() => {
                    void start();
                  }}
                >
                  Activer la caméra
                </Button>
              </>
            )}
          </div>
        ) : null}
      </div>

      <p aria-live="polite" className={styles.lead}>
        {phase === 'running'
          ? 'Cadrez le code-barres dans le rectangle.'
          : phase === 'starting'
            ? 'Démarrage de la caméra…'
            : ''}
      </p>

      {error ? (
        <Callout tone="danger" title="Caméra indisponible">
          <p>{error}</p>
        </Callout>
      ) : null}

      {struggling ? (
        <Callout tone="warn" title="Le code ne se lit pas ?">
          <p>
            Rapprochez-vous, évitez les reflets sur le film plastique, ou saisissez le code à la
            main ci-dessous.
          </p>
        </Callout>
      ) : null}

      <div className={styles.scannerActions}>
        {phase === 'running' && torchAvailable ? (
          <Button variant="secondary" onClick={toggleTorch} aria-pressed={torchOn}>
            {torchOn ? 'Éteindre la lampe' : 'Allumer la lampe'}
          </Button>
        ) : null}
        {phase === 'error' ? (
          <Button
            variant="secondary"
            onClick={() => {
              void start();
            }}
          >
            Réessayer
          </Button>
        ) : null}
        <Button variant="secondary" onClick={onManualEntry}>
          Saisie manuelle
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            release();
            onCancel();
          }}
        >
          Annuler
        </Button>
      </div>

      <div className={styles.gtinEntry}>
        <Field
          label="Ou tapez les chiffres du code-barres"
          hint="8, 12, 13 ou 14 chiffres. La clé de contrôle est vérifiée sur l’appareil."
          error={gtinError}
        >
          {({ id, describedBy, invalid }) => (
            <input
              id={id}
              aria-describedby={describedBy}
              aria-invalid={invalid}
              className={controlClass(invalid)}
              type="text"
              inputMode="numeric"
              autoComplete="off"
              value={gtinInput}
              onChange={(event) => {
                setGtinInput(event.target.value);
                setGtinError(null);
              }}
            />
          )}
        </Field>
        <Button variant="secondary" onClick={submitGtin} disabled={gtinInput.trim() === ''}>
          Chercher ce code
        </Button>
      </div>
    </div>
  );
}
