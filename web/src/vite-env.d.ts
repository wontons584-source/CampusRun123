/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 'true' 时走 src/api/mock.ts 的内存假数据 */
  readonly VITE_USE_MOCK?: string;
  /** 真实接口基址，留空表示同源 /api */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
