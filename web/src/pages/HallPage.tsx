/**
 * 抢单大厅（`/hall`）—— 录屏主战场。
 *
 * 两个 Tab：
 * - 抢单大厅：别人发的 PENDING 单，卡片带倒计时 + 抢单按钮
 * - 我的订单：我发的 + 我接的，按状态分组
 * 右下角悬浮 `+ 发单`。
 */

import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, isMockMode } from '../api/client';
import { isAuthError, toErrorMessage } from '../api/errors';
import { AppShell } from '../components/AppShell';
import { OrderCard } from '../components/OrderCard';
import { useToast } from '../components/Toast';
import { Button, EmptyState, SkeletonList } from '../components/ui';
import { statusGroup } from '../lib/format';
import type { StatusGroup } from '../lib/format';
import { useAuth } from '../state/AuthContext';
import type { HallOrder } from '../types';

type Tab = 'hall' | 'mine';

/** 我的订单分组（顺序 = 录屏时从上到下的阅读顺序；口径见 PRD 7.1 的 4 组折叠）。 */
const MY_ORDER_GROUPS: ReadonlyArray<{ key: StatusGroup; label: string }> = [
  { key: 'PENDING', label: '待接单' },
  { key: 'IN_PROGRESS', label: '进行中' },
  { key: 'COMPLETED', label: '已完成' },
  { key: 'CLOSED', label: '已取消 / 已流单' },
];

/** Tab 按钮。 */
function TabButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 border-b-2 py-2.5 text-sm ${
        active ? 'border-primary font-semibold text-primary' : 'border-transparent text-gray-500'
      }`}
    >
      {children}
    </button>
  );
}

/** 大厅页。 */
export function HallPage(): JSX.Element {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();

  const [tab, setTab] = useState<Tab>('hall');
  const [hallOrders, setHallOrders] = useState<HallOrder[]>([]);
  const [myOrders, setMyOrders] = useState<HallOrder[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [grabbingId, setGrabbingId] = useState<number | null>(null);

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

  const loadAll = useCallback(
    async (silent: boolean): Promise<void> => {
      if (silent) {
        setRefreshing(true);
      } else {
        setLoading(true);
      }
      try {
        const [hall, mine] = await Promise.all([api.hall(), api.myOrders()]);
        setHallOrders(hall);
        setMyOrders(mine);
      } catch (error) {
        handleAuthFailure(error);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [handleAuthFailure],
  );

  useEffect(() => {
    void loadAll(false);
  }, [loadAll]);

  const handleGrab = async (orderId: number): Promise<void> => {
    if (grabbingId !== null) {
      return;
    }
    setGrabbingId(orderId);
    try {
      await api.grabOrder(orderId);
      toast.success('抢单成功，快去取件吧');
      navigate(`/orders/${orderId}`);
    } catch (error) {
      handleAuthFailure(error);
      // 抢单失败常见原因是「手慢了」，刷新一次列表让状态对齐
      void loadAll(true);
    } finally {
      setGrabbingId(null);
    }
  };

  const renderHall = (): JSX.Element => {
    if (loading) {
      return <SkeletonList rows={3} />;
    }
    if (hallOrders.length === 0) {
      return (
        <EmptyState
          title="暂时没有待接的单"
          description="点「刷新」再看看，或者自己发一单试试"
        />
      );
    }
    return (
      <div className="space-y-3">
        {hallOrders.map((order) => (
          <OrderCard
            key={order.id}
            order={order}
            onOpen={() => {
              navigate(`/orders/${order.id}`);
            }}
            onGrab={() => {
              void handleGrab(order.id);
            }}
            grabbing={grabbingId === order.id}
          />
        ))}
      </div>
    );
  };

  const renderMine = (): JSX.Element => {
    if (loading) {
      return <SkeletonList rows={3} />;
    }
    if (myOrders.length === 0) {
      return <EmptyState title="还没有订单" description="去发一单，或到大厅抢一单" />;
    }

    const sections = MY_ORDER_GROUPS.map((group) => ({
      label: group.label,
      orders: myOrders.filter((order) => statusGroup(order.status) === group.key),
    })).filter((section) => section.orders.length > 0);

    return (
      <div className="space-y-5">
        {sections.map((section) => (
          <section key={section.label}>
            <h2 className="mb-2 text-xs font-medium text-gray-400">
              {section.label}（{section.orders.length}）
            </h2>
            <div className="space-y-3">
              {section.orders.map((order) => {
                // 优先用后端权威的 my_role；老接口没有该字段时退回 publisher_id 推断
                const role: 'publisher' | 'runner' | undefined =
                  order.my_role === 'PUBLISHER'
                    ? 'publisher'
                    : order.my_role === 'RUNNER'
                      ? 'runner'
                      : order.publisher_id === undefined
                        ? undefined
                        : order.publisher_id === user?.id
                          ? 'publisher'
                          : 'runner';
                return (
                  <OrderCard
                    key={order.id}
                    order={order}
                    role={role}
                    onOpen={() => {
                      navigate(`/orders/${order.id}`);
                    }}
                  />
                );
              })}
            </div>
          </section>
        ))}
      </div>
    );
  };

  return (
    <AppShell bottomSpace>
      {/* 头部：昵称 + 登出 + 刷新 */}
      <div className="mb-4 flex items-center justify-between">
        <div className="min-w-0">
          <p className="text-xs text-gray-400">你好</p>
          <p className="truncate text-lg font-semibold text-gray-900">{user?.nickname ?? '同学'}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {isMockMode ? (
            <span className="rounded-md bg-orange-50 px-2 py-1 text-[11px] font-medium text-orange-600">
              MOCK
            </span>
          ) : null}
          <Button
            variant="secondary"
            size="sm"
            loading={refreshing}
            onClick={() => {
              void loadAll(true);
            }}
          >
            刷新
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              logout();
              navigate('/login', { replace: true });
            }}
          >
            退出
          </Button>
        </div>
      </div>

      {/* Tabs */}
      <div className="mb-4 flex border-b border-gray-200">
        <TabButton active={tab === 'hall'} onClick={() => setTab('hall')}>
          抢单大厅
        </TabButton>
        <TabButton active={tab === 'mine'} onClick={() => setTab('mine')}>
          我的订单
        </TabButton>
      </div>

      {tab === 'hall' ? renderHall() : renderMine()}

      {/* 悬浮发单按钮：跟随 480px 容器右对齐 */}
      <div className="pointer-events-none fixed inset-x-0 bottom-0 z-20 mx-auto max-w-app px-4 pb-5">
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => {
              navigate('/create');
            }}
            className="pointer-events-auto flex items-center gap-1.5 rounded-full bg-primary px-4 py-3 text-sm font-medium text-white shadow-lg active:bg-primary-dark"
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="M10 4v12M4 10h12" strokeLinecap="round" />
            </svg>
            发单
          </button>
        </div>
      </div>
    </AppShell>
  );
}
