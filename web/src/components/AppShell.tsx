/**
 * 移动端优先的应用外壳：内容区最大 480px、居中、浅灰背景。
 *
 * 所有页面共用，保证录屏时不同页面的宽度/留白完全一致。
 */

import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

interface AppShellProps {
  children: ReactNode;
  /** 顶部标题；不传则不显示标题栏 */
  title?: string;
  /** 是否显示返回按钮 */
  showBack?: boolean;
  /** 标题栏右侧插槽 */
  right?: ReactNode;
  /** 是否留出底部悬浮按钮的空间 */
  bottomSpace?: boolean;
}

/** 页面外壳。 */
export function AppShell({
  children,
  title,
  showBack = false,
  right,
  bottomSpace = false,
}: AppShellProps): JSX.Element {
  const navigate = useNavigate();

  return (
    <div className="min-h-screen bg-gray-100">
      <div className={`mx-auto flex min-h-screen max-w-app flex-col ${bottomSpace ? 'pb-24' : 'pb-6'}`}>
        {title !== undefined ? (
          <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b border-gray-200 bg-white px-4">
            <div className="flex min-w-0 items-center gap-2">
              {showBack ? (
                <button
                  type="button"
                  aria-label="返回"
                  onClick={() => {
                    navigate(-1);
                  }}
                  className="-ml-1 flex h-8 w-8 items-center justify-center rounded-lg text-gray-600 active:bg-gray-100"
                >
                  <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M12 5l-5 5 5 5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              ) : null}
              <h1 className="truncate text-base font-semibold text-gray-900">{title}</h1>
            </div>
            <div className="flex shrink-0 items-center gap-2">{right}</div>
          </header>
        ) : null}
        <main className="flex-1 px-4 pt-4">{children}</main>
      </div>
    </div>
  );
}
