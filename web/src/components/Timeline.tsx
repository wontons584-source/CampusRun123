/**
 * 竖直时间轴（订单详情页的核心视觉）。
 *
 * 6 个节点：发单 → 接单 → 取件 → 配送中 → 送达 → 完成。
 * 已完成：主色实心圆 + 勾；当前节点：主色描边 + 淡底；未到达：灰色空心。
 * 刻意不用任何 CSS transition/animation —— 录屏时卡顿比朴素更难接受。
 */

import { formatTime } from '../lib/format';
import type { TimelineNode } from '../types';

interface TimelineProps {
  nodes: TimelineNode[];
}

/** 渲染单个节点的圆点。 */
function Dot({ done, current }: { done: boolean; current: boolean }): JSX.Element {
  if (done) {
    return (
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary text-white">
        <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.5">
          <path d="M4 10.5l4 4 8-8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
    );
  }
  if (current) {
    return (
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 border-primary bg-primary-light">
        <span className="h-2 w-2 rounded-full bg-primary" />
      </span>
    );
  }
  return (
    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 border-gray-200 bg-white">
      <span className="h-1.5 w-1.5 rounded-full bg-gray-300" />
    </span>
  );
}

/** 时间轴主体。 */
export function Timeline({ nodes }: TimelineProps): JSX.Element {
  return (
    <ol className="space-y-0">
      {nodes.map((node, index) => {
        const isLast = index === nodes.length - 1;
        const active = node.done;
        return (
          <li key={node.key} className="flex gap-3">
            <div className="flex flex-col items-center">
              <Dot done={node.done} current={node.current} />
              {!isLast ? (
                <span
                  className={`w-0.5 flex-1 ${node.done ? 'bg-primary/40' : 'bg-gray-200'}`}
                  style={{ minHeight: '28px' }}
                />
              ) : null}
            </div>
            <div className={`flex-1 ${isLast ? 'pb-0' : 'pb-5'}`}>
              <div className="flex items-baseline justify-between">
                <span
                  className={`text-sm ${
                    active ? 'font-semibold text-gray-900' : node.current ? 'font-medium text-primary' : 'text-gray-400'
                  }`}
                >
                  {node.label}
                </span>
                <span className="text-xs text-gray-400">{node.at === null ? '' : formatTime(node.at)}</span>
              </div>
              {node.current ? <p className="mt-0.5 text-xs text-primary">当前进度</p> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
