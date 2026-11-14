/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_OHIF_VIEWER_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
