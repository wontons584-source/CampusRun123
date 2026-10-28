/**
 * 路由守卫：未登录（且 token 恢复失败）时重定向到 `/login`。
 *
 * 首次进站会先等 AuthContext 用 token 拉一次 `/me`，避免刷新页面时闪一下登录页。
 */

import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { LoadingHint } from '../components/ui';
import { useAuth } from '../state/AuthContext';

/** 受保护路由包装。 */
export function RequireAuth({ children }: { children: ReactNode }): JSX.Element {
  const { authenticated, initializing } = useAuth();
  const location = useLocation();

  if (initializing) {
    return (
      <div className="mx-auto flex min-h-screen max-w-app items-center justify-center bg-gray-100">
        <LoadingHint text="正在恢复登录状态…" />
      </div>
    );
  }

  if (!authenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
}
