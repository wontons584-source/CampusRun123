/**
 * 应用根组件：Provider + 路由表。
 *
 * 路由：
 * - `/login`    登录 / 注册
 * - `/hall`     抢单大厅 + 我的订单（默认落地页）
 * - `/create`   发单
 * - `/orders/:id` 订单详情
 */

import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';

import { RequireAuth } from './components/RequireAuth';
import { ToastProvider } from './components/Toast';
import { CreatePage } from './pages/CreatePage';
import { HallPage } from './pages/HallPage';
import { LoginPage } from './pages/LoginPage';
import { OrderDetailPage } from './pages/OrderDetailPage';
import { AuthProvider } from './state/AuthContext';

/** 根组件。 */
export function App(): JSX.Element {
  return (
    <AuthProvider>
      <ToastProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/hall"
              element={
                <RequireAuth>
                  <HallPage />
                </RequireAuth>
              }
            />
            <Route
              path="/create"
              element={
                <RequireAuth>
                  <CreatePage />
                </RequireAuth>
              }
            />
            <Route
              path="/orders/:id"
              element={
                <RequireAuth>
                  <OrderDetailPage />
                </RequireAuth>
              }
            />
            <Route path="/" element={<Navigate to="/hall" replace />} />
            <Route path="*" element={<Navigate to="/hall" replace />} />
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  );
}

export default App;
