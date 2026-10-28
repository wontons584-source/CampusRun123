/**
 * 登录态上下文。
 *
 * 只做三件事：持有 `token + user`、登录/注册后写入、登出清空。
 * 不引入状态管理库（任务约束），一个 Context + useState 足够。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { api } from '../api/client';
import { clearToken, getToken, setToken } from '../api/token';
import type { AuthResult, User } from '../types';

interface AuthContextValue {
  /** 当前用户；未登录为 null */
  user: User | null;
  /** 是否还在恢复登录态（首次进站会自动拉 /me） */
  initializing: boolean;
  /** 是否已登录（有 token） */
  authenticated: boolean;
  /** 登录 */
  login: (phone: string, password: string) => Promise<User>;
  /** 注册 */
  register: (nickname: string, phone: string, password: string) => Promise<User>;
  /** 登出 */
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * 把 `AuthResult` 落地：写 token + 更新内存用户。
 */
function applyAuthResult(result: AuthResult): User {
  setToken(result.token);
  return result.user;
}

/** Provider：挂在 App 最外层。 */
export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState<boolean>(getToken() !== null);

  // 首次挂载：有 token 就尝试恢复用户信息，失败则视为未登录
  useEffect(() => {
    const token = getToken();
    if (token === null) {
      setInitializing(false);
      return;
    }

    let cancelled = false;
    void (async () => {
      try {
        const me = await api.me();
        if (!cancelled) {
          setUser(me);
        }
      } catch {
        if (!cancelled) {
          clearToken();
          setUser(null);
        }
      } finally {
        if (!cancelled) {
          setInitializing(false);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (phone: string, password: string): Promise<User> => {
    const result = await api.login({ phone, password });
    const nextUser = applyAuthResult(result);
    setUser(nextUser);
    return nextUser;
  }, []);

  const register = useCallback(
    async (nickname: string, phone: string, password: string): Promise<User> => {
      const result = await api.register({ nickname, phone, password });
      const nextUser = applyAuthResult(result);
      setUser(nextUser);
      return nextUser;
    },
    [],
  );

  const logout = useCallback((): void => {
    clearToken();
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      initializing,
      authenticated: user !== null,
      login,
      register,
      logout,
    }),
    [user, initializing, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** 读取登录态；必须在 `AuthProvider` 内使用。 */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (context === null) {
    throw new Error('useAuth 必须在 AuthProvider 内部使用');
  }
  return context;
}
