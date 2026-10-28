/**
 * 订单详情页（`/orders/:id`）—— 录屏第二个主战场。
 *
 * 结构：订单头（订单号/状态/酬劳）→ 竖直时间轴 → 明细 → 底部动态操作栏。
 * 操作按钮完全由后端下发的 `available_actions[]` 驱动，前端不猜权限：
 * ACCEPT / PICKED_UP / ON_THE_WAY / DELIVER / CONFIRM。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api } from '../api/client';
import { isAuthError, isStaleStateError, toErrorMessage } from '../api/errors';
import { AppShell } from '../components/AppShell';
import { Countdown } from '../components/Countdown';
import { Timeline } from '../components/Timeline';
import { useToast } from '../components/Toast';
import { Badge, Button, Card, LoadingHint } from '../components/ui';
import { useAuth } from '../state/AuthContext';
import {
  actionLabel,
  expectMinutesLabel,
  fallbackTimeline,
  formatMoney,
  formatTime,
  orderTypeLabel,
  statusLabel,
  statusTone,
} from '../lib/format';
import type { ActionCode, OrderDetail, ProgressAction } from '../types';

/** 动作成功后的提示文案。 */
const SUCCESS_TEXT: Record<ActionCode, string> = {
  ACCEPT: '抢单成功，记得尽快取件',
  PICKED_UP: '已标记取件，开始配送吧',
  ON_THE_WAY: '已更新为配送中',
  DELIVER: '已标记送达，等待对方确认',
  CONFIRM: '确认收货成功，订单完成',
};

/** 一行「标签 - 值」。 */
function InfoRow({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <span className="shrink-0 text-sm text-gray-500">{label}</span>
      <span className="text-right text-sm text-gray-900">{value}</span>
    </div>
  );
}

/** 详情页。 */
export function OrderDetailPage(): JSX.Element {
  const params = useParams<{ id: string }>();
  const orderId = Number(params.id);
  const navigate = useNavigate();
  const toast = useToast();
  const { user, logout } = useAuth();

  const [detail, setDetail] = useState<OrderDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [pendingAction, setPendingAction] = useState<ActionCode | null>(null);

  const handleAuthFailure = useCallback(
    (error: unknown): void => {
      if (isAuthError(error)) {
        logout();
        navigate('/login', { replace: true });
        return;
      }
      toast.error(toErrorMessage(error));
    },
    [logout, navigate, toast],
  );

  const load = useCallback(
    async (silent: boolean): Promise<void> => {
      if (!Number.isFinite(orderId) || orderId <= 0) {
        toast.error('订单号不合法');
        navigate('/hall', { replace: true });
        return;
      }
      if (!silent) {
        setLoading(true);
      }
      try {
        const data = await api.orderDetail(orderId);
        setDetail(data);
      } catch (error) {
        handleAuthFailure(error);
      } finally {
        if (!silent) {
          setLoading(false);
        }
      }
    },
    [handleAuthFailure, navigate, orderId, toast],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  const timelineNodes = useMemo(() => {
    if (detail === null) {
      return [];
    }
    if (detail.timeline !== undefined && detail.timeline.length > 0) {
      return detail.timeline;
    }
    return fallbackTimeline(detail.status);
  }, [detail]);

  const runAction = async (action: ActionCode): Promise<void> => {
    if (pendingAction !== null) {
      return;
    }
    setPendingAction(action);
    try {
      if (action === 'ACCEPT') {
        await api.grabOrder(orderId);
      } else if (action === 'CONFIRM') {
        await api.confirm(orderId);
      } else {
        await api.progress(orderId, action as ProgressAction);
      }
      // 成功后统一重新拉详情，保证按钮/时间轴与后端状态机一致
      const fresh = await api.orderDetail(orderId);
      setDetail(fresh);
      toast.success(SUCCESS_TEXT[action]);
    } catch (error) {
      if (isAuthError(error)) {
        handleAuthFailure(error);
      } else if (isStaleStateError(error)) {
        // 40905（CAS 失败/别人先动了）与 42201（非法迁移/跳级）都意味着页面状态过期
        toast.error('订单状态已变化，已为你刷新');
        void load(true);
      } else {
        handleAuthFailure(error);
        void load(true);
      }
    } finally {
      setPendingAction(null);
    }
  };

  const renderEmptyHint = (order: OrderDetail): string => {
    if (order.status === 'COMPLETED') {
      return '订单已完成';
    }
    if (order.status === 'CANCELLED') {
      return '订单已取消';
    }
    if (order.status === 'EXPIRED') {
      return '订单已流单（无人接单）';
    }
    if (order.status === 'DISPUTED') {
      return '订单纠纷处理中，暂不可操作';
    }
    if (order.status === 'PENDING') {
      return order.publisher_id === user?.id ? '等待其他同学接单…' : '当前无可执行操作';
    }
    if (order.status === 'DELIVERED') {
      return order.publisher_id === user?.id
        ? '已送达，请确认收货'
        : '已送达，等待发单人确认收货…';
    }
    return '当前无可执行操作';
  };

  if (loading) {
    return (
      <AppShell title="订单详情" showBack>
        <LoadingHint text="正在加载订单…" />
      </AppShell>
    );
  }

  if (detail === null) {
    return (
      <AppShell title="订单详情" showBack>
        <Card>
          <p className="py-8 text-center text-sm text-gray-500">订单不存在或加载失败</p>
          <Button
            block
            variant="secondary"
            onClick={() => {
              void load(false);
            }}
          >
            重新加载
          </Button>
        </Card>
      </AppShell>
    );
  }

  const isPublisher = detail.publisher_id === user?.id;
  const actions = detail.available_actions;

  return (
    <AppShell
      title="订单详情"
      showBack
      bottomSpace
      right={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            void load(true);
          }}
        >
          刷新
        </Button>
      }
    >
      <div className="space-y-4">
        {/* 订单头 */}
        <Card>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs text-gray-400">订单号</p>
              <p className="mt-0.5 truncate font-mono text-sm text-gray-700">{detail.order_no}</p>
            </div>
            <Badge tone={statusTone(detail.status)}>{statusLabel(detail.status)}</Badge>
          </div>

          <div className="mt-3 flex items-end justify-between border-t border-gray-100 pt-3">
            <div>
              <p className="text-xs text-gray-400">酬劳</p>
              <p className="text-2xl font-semibold text-gray-900">{formatMoney(detail.reward)}</p>
            </div>
            <div className="text-right">
              <p className="text-xs text-gray-400">
                {detail.status === 'PENDING' ? '剩余接单时间' : '期望送达'}
              </p>
              <Countdown
                deadline={detail.deadline}
                className="font-mono text-sm font-medium text-orange-600"
                expiredText="已超时"
              />
            </div>
          </div>
        </Card>

        {/* 时间轴 */}
        <Card>
          <h2 className="mb-4 text-sm font-semibold text-gray-900">订单进度</h2>
          <Timeline nodes={timelineNodes} />
        </Card>

        {/* 明细 */}
        <Card>
          <h2 className="mb-1 text-sm font-semibold text-gray-900">任务信息</h2>
          <div className="divide-y divide-gray-50">
            <InfoRow label="任务类型" value={orderTypeLabel(detail.type)} />
            <InfoRow label="起点" value={detail.from_poi_name} />
            <InfoRow label="终点" value={detail.to_poi_name} />
            <InfoRow label="期望时长" value={expectMinutesLabel(detail.expect_minutes)} />
            <InfoRow label="发单人" value={detail.publisher_name ?? (isPublisher ? '我' : '同学')} />
            <InfoRow
              label="接单人"
              value={
                detail.runner_id === null || detail.runner_id === undefined
                  ? '待接单'
                  : detail.runner_name ?? (detail.runner_id === user?.id ? '我' : '同学')
              }
            />
            <InfoRow label="发布时间" value={formatTime(detail.created_at)} />
            {detail.pick_code !== undefined && detail.pick_code !== null && detail.pick_code !== '' ? (
              <InfoRow label="取件码" value={detail.pick_code} />
            ) : null}
            {detail.description !== undefined && detail.description !== null && detail.description !== '' ? (
              <div className="py-2">
                <p className="text-sm text-gray-500">描述</p>
                <p className="mt-1 whitespace-pre-wrap text-sm text-gray-900">{detail.description}</p>
              </div>
            ) : null}
          </div>
        </Card>

        <p className="pb-2 text-center text-xs text-gray-400">
          {isPublisher ? '你是本单的发单人' : '你是本单的接单人'}
        </p>
      </div>

      {/* 底部动态操作栏 */}
      <div className="pointer-events-none fixed inset-x-0 bottom-0 z-30 mx-auto max-w-app border-t border-gray-200 bg-white px-4 py-3">
        <div className="pointer-events-auto flex gap-2">
          {actions.length === 0 ? (
            <p className="flex-1 py-2.5 text-center text-sm text-gray-400">{renderEmptyHint(detail)}</p>
          ) : (
            actions.map((action) => (
              <Button
                key={action}
                block
                size="lg"
                loading={pendingAction === action}
                disabled={pendingAction !== null && pendingAction !== action}
                onClick={() => {
                  void runAction(action);
                }}
              >
                {actionLabel(action)}
              </Button>
            ))
          )}
        </div>
      </div>
    </AppShell>
  );
}
