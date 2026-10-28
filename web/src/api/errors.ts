/**
 * 前端统一业务异常。
 *
 * 单独成文件是为了打断循环依赖：`client.ts` 与 `mock.ts` 都要抛同一个类，
 * 而 `client.ts` 又要 import `mock.ts`。
 */
export class ApiError extends Error {
  /** 后端 `code`（非 0）；网络层失败时为 -1，HTTP 层解析失败时为 HTTP status */
  readonly code: number;

  constructor(message: string, code: number) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    // 兼容 ES5 target 下的 instanceof 判定
    Object.setPrototypeOf(this, ApiError.prototype);
  }
}

/** 是否为鉴权类错误（401xx）——调用方据此决定是否踢回登录页。 */
export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.code >= 40100 && err.code < 40200;
}

/**
 * 是否为「状态已被并发改动 / 非法迁移」。
 *
 * 后端约定：
 * - `40905`：CAS 失败，别人先动了 → 前端应提示「刷新重试」而不是「操作失败」；
 * - `42201`：非法迁移（含跳级，如 `PICKED_UP → DELIVER`）。
 * 两者都意味着「页面上的状态过期了」，统一处理成「刷新详情」。
 */
export function isStaleStateError(err: unknown): boolean {
  return err instanceof ApiError && (err.code === 40905 || err.code === 42201);
}

/** 把任意 catch 到的异常转成可直接展示的中文文案。 */
export function toErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return err.message;
  }
  if (err instanceof Error && err.message) {
    return err.message;
  }
  return '操作失败，请稍后重试';
}
