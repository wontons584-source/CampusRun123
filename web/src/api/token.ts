/**
 * token 持久化（localStorage）。
 *
 * 单独成文件：`client.ts` 与 `mock.ts` 都要读写 token，抽出来避免循环依赖。
 */

const TOKEN_KEY = 'campusrun.token';

/** 读取 token；没有返回 null。 */
export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

/** 写入 token。 */
export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // 隐私模式下 localStorage 可能不可写，失败不影响本次会话（内存态仍可用）
  }
}

/** 清空 token（登出 / 40100）。 */
export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // 同上，忽略
  }
}
