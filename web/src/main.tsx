/**
 * 入口：挂载 React 应用。
 *
 * 不引任何 UI 库 / 状态管理库，bundle 只有 react + react-router-dom + 业务代码。
 */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import './index.css';

const container = document.getElementById('root');
if (container === null) {
  throw new Error('找不到 #root 挂载点，请检查 index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
