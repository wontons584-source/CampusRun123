/**
 * 订单卡片（大厅列表 / 我的订单列表共用）。
 *
 * 一张卡只放 4 件事：类型 + 状态、起点→终点、酬劳、主操作（抢单 / 进入详情）。
 */

import { Countdown } from './Countdown';
import { Badge, Button } from './ui';
import { formatMoney, orderTypeLabel, statusLabel, statusTone } from '../lib/format';
import type { HallOrder } from '../types';

interface OrderCardProps {
  order: HallOrder;
  /** 我在本单里的角色，用于「我的订单」分组展示 */
  role?: 'publisher' | 'runner' | undefined;
  /** 点卡片进入详情 */
  onOpen: () => void;
  /** 大厅里点「抢单」；不传则不显示抢单按钮 */
  onGrab?: (() => void) | undefined;
  /** 抢单请求进行中 */
  grabbing?: boolean;
}

/** 一行地点：起点 → 终点。 */
function RouteRow({ from, to }: { from: string; to: string }): JSX.Element {
  return (
    <div className="mt-2 flex items-center gap-2 text-sm text-gray-900">
      <span className="truncate">{from}</span>
      <svg viewBox="0 0 20 20" className="h-3.5 w-3.5 shrink-0 text-gray-400" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M4 10h12M12 6l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="truncate">{to}</span>
    </div>
  );
}

/** 订单卡片。 */
export function OrderCard({ order, role, onOpen, onGrab, grabbing = false }: OrderCardProps): JSX.Element {
  const showGrab = typeof onGrab === 'function' && order.available_actions.includes('ACCEPT');
  const roleText = role === 'publisher' ? '我发的' : role === 'runner' ? '我接的' : null;

  return (
    <div className="rounded-xl border border-gray-100 bg-white p-4 shadow-sm">
      <div
        onClick={onOpen}
        className="cursor-pointer"
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            onOpen();
          }
        }}
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Badge tone="gray">{order.type === undefined ? '任务' : orderTypeLabel(order.type)}</Badge>
            <Badge tone={statusTone(order.status)}>{statusLabel(order.status)}</Badge>
            {roleText !== null ? (
              <span className="text-xs text-gray-400">{roleText}</span>
            ) : null}
          </div>
          {order.status === 'PENDING' ? (
            <span className="shrink-0 text-xs text-gray-500">
              剩余 <Countdown deadline={order.deadline} className="font-mono font-medium text-orange-600" />
            </span>
          ) : null}
        </div>

        <RouteRow from={order.from_poi_name} to={order.to_poi_name} />

        {order.description !== undefined && order.description !== null && order.description !== '' ? (
          <p className="mt-2 line-clamp-2 text-xs text-gray-400">{order.description}</p>
        ) : null}
      </div>

      <div className="mt-3 flex items-end justify-between border-t border-gray-100 pt-3">
        <div>
          <p className="text-xs text-gray-400">酬劳</p>
          <p className="text-xl font-semibold text-gray-900">{formatMoney(order.reward)}</p>
        </div>
        {showGrab ? (
          <Button
            size="md"
            loading={grabbing}
            onClick={() => {
              onGrab?.();
            }}
          >
            抢单
          </Button>
        ) : (
          <Button variant="secondary" size="md" onClick={onOpen}>
            查看详情
          </Button>
        )}
      </div>
    </div>
  );
}
