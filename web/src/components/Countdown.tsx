/**
 * 实时倒计时（每秒 tick 一次，卸载即清理定时器）。
 *
 * 卡片列表里会同时存在多个实例，所以只在 `mounted` 且未超时时跑 interval，
 * 超时后自动停表——避免录屏时后台空转。
 */

import { useEffect, useState } from 'react';

import { formatCountdown, remainingMs } from '../lib/format';

interface CountdownProps {
  deadline: string;
  className?: string;
  /** 超时后的文案 */
  expiredText?: string;
}

/** 倒计时文本。 */
export function Countdown({ deadline, className = '', expiredText = '已超时' }: CountdownProps): JSX.Element {
  const [text, setText] = useState<string>(() =>
    remainingMs(deadline) > 0 ? formatCountdown(deadline) : expiredText,
  );

  useEffect(() => {
    const tick = (): void => {
      const left = remainingMs(deadline);
      if (left <= 0) {
        setText(expiredText);
        return;
      }
      setText(formatCountdown(deadline));
    };

    tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [deadline, expiredText]);

  return <span className={className}>{text}</span>;
}
