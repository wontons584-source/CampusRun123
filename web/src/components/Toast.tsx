/**
 * 极简 toast（约 20 行核心逻辑，不引第三方库）。
 *
 * 用法：
 * ```tsx
 * const toast = useToast();
 * toast.error('抢单失败：手慢了');
 * ```
 * 顶部居中浮层，2.4 秒自动消失，不阻断点击。
 */

import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

type ToastType = 'error' | 'success' | 'info';

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
}

interface ToastContextValue {
  show: (message: string, type?: ToastType) => void;
  error: (message: string) => void;
  success: (message: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

/** 展示时长：后端 message 一般 10–20 字，3.2s 足够读完又不挡操作。 */
const DURATION_MS = 3200;

const TONE_CLASS: Record<ToastType, string> = {
  error: 'bg-red-600',
  success: 'bg-green-600',
  info: 'bg-gray-800',
};

/** Provider：挂在 App 最外层。 */
export function ToastProvider({ children }: { children: ReactNode }): JSX.Element {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef<number>(1);

  const show = useCallback((message: string, type: ToastType = 'info'): void => {
    const id = nextId.current;
    nextId.current += 1;
    setItems((prev) => [...prev, { id, message, type }]);
    window.setTimeout(() => {
      setItems((prev) => prev.filter((item) => item.id !== id));
    }, DURATION_MS);
  }, []);

  const error = useCallback((message: string): void => show(message, 'error'), [show]);
  const success = useCallback((message: string): void => show(message, 'success'), [show]);

  const value = useMemo<ToastContextValue>(() => ({ show, error, success }), [show, error, success]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 top-3 z-50 flex flex-col items-center gap-2 px-4">
        {items.map((item) => (
          <div
            key={item.id}
            className={`pointer-events-auto max-w-app rounded-lg px-4 py-2.5 text-sm leading-snug text-white shadow-lg ${TONE_CLASS[item.type]}`}
            role="status"
          >
            {item.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** 读取 toast；必须在 `ToastProvider` 内使用。 */
export function useToast(): ToastContextValue {
  const context = useContext(ToastContext);
  if (context === null) {
    throw new Error('useToast 必须在 ToastProvider 内部使用');
  }
  return context;
}
