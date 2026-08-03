/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_HOUSEHOLD_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/**
 * Torch (and its capability flag) is standardised in the MediaStream Image
 * Capture spec but absent from TypeScript's DOM lib. Chrome on Android exposes
 * it; Safari on iOS never does — which is why the UI probes `getCapabilities()`
 * rather than assuming it exists.
 */
interface MediaTrackCapabilities {
  torch?: boolean;
}

interface MediaTrackConstraintSet {
  torch?: ConstrainBoolean;
}

/**
 * zxing-wasm ships the reader binary as a package export. Importing it with
 * `?url` makes Vite emit it as a build asset, so it is served from our own
 * origin and precached by the service worker instead of being fetched from a
 * CDN at first scan.
 */
declare module 'zxing-wasm/reader/zxing_reader.wasm?url' {
  const url: string;
  export default url;
}
