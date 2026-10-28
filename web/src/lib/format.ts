/**
 * 展示层格式化工具。集中放这里，避免每个页面各写一份 time/price 逻辑。
 */

import type { ActionCode, OrderStatus, OrderType, TimelineNode } from '../types';

/** 金额归一化：后端 `Decimal` → string，mock → number。 */
export function toNumber(value: number | string | null | undefined): number {
  if (value === null || value === undefined) {
    return 0;
  }
  const parsed = typeof value === 'number' ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * 金额展示。
 *
 * 后端 `reward` 是 `Decimal → str`（"5.50" / "12.30"），**字符串直接透传**能保住尾零，
 * 也不经过浮点；只有 mock 给 number 时才走数字格式化（整数去掉 .0）。
 */
export function formatMoney(value: number | string | null | undefined): string {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (/^\d+(\.\d+)?$/.test(trimmed)) {
      return `¥${trimmed}`;
    }
  }
  const amount = toNumber(value);
  const text = Number.isInteger(amount) ? amount.toFixed(0) : amount.toFixed(1);
  return `¥${text}`;
}

/** ISO 时间 → `HH:mm`；非法输入返回 `--:--`。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) {
    return '--:--';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return '--:--';
  }
  const hh = `${date.getHours()}`.padStart(2, '0');
  const mm = `${date.getMinutes()}`.padStart(2, '0');
  return `${hh}:${mm}`;
}

/** 剩余毫秒数（<=0 表示已超时）。 */
export function remainingMs(deadline: string | null | undefined): number {
  if (!deadline) {
    return 0;
  }
  const target = new Date(deadline).getTime();
  if (Number.isNaN(target)) {
    return 0;
  }
  return target - Date.now();
}

/** 倒计时文案：`12:34` / `已超时`。 */
export function formatCountdown(deadline: string | null | undefined): string {
  const ms = remainingMs(deadline);
  if (ms <= 0) {
    return '已超时';
  }
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${`${minutes}`.padStart(2, '0')}:${`${seconds}`.padStart(2, '0')}`;
}

/** 订单状态 → 中文文案。 */
export function statusLabel(status: OrderStatus): string {
  switch (status) {
    case 'PENDING':
      return '待接单';
    case 'IN_PROGRESS':
      return '进行中';
    case 'DELIVERED':
      return '已送达';
    case 'COMPLETED':
      return '已完成';
    case 'CANCELLED':
      return '已取消';
    case 'EXPIRED':
      return '已流单';
    case 'DISPUTED':
      return '纠纷中';
    default:
      return status;
  }
}

/** 订单状态 → 语义色（待接单蓝 / 进行中橙 / 完成绿 / 取消灰）。 */
export function statusTone(status: OrderStatus): 'blue' | 'orange' | 'green' | 'gray' | 'red' {
  switch (status) {
    case 'PENDING':
      return 'blue';
    case 'IN_PROGRESS':
    case 'DELIVERED':
      return 'orange';
    case 'COMPLETED':
      return 'green';
    case 'DISPUTED':
      return 'red';
    case 'CANCELLED':
    case 'EXPIRED':
    default:
      return 'gray';
  }
}

/** 列表分组（PRD 7.1「对外折叠 4 种」）：7 个状态折叠成 4 组。 */
export type StatusGroup = 'PENDING' | 'IN_PROGRESS' | 'COMPLETED' | 'CLOSED';

/**
 * 状态 → 分组。
 *
 * PRD 7.1 的折叠口径：`IN_PROGRESS / DELIVERED / DISPUTED` 都归「进行中」，
 * `CANCELLED / EXPIRED` 归「已取消」。UI 分组必须走这个函数，
 * 不要直接拿 `status === 'IN_PROGRESS'` 判断，否则 DELIVERED 的单会掉进"已完成"。
 */
export function statusGroup(status: OrderStatus): StatusGroup {
  switch (status) {
    case 'PENDING':
      return 'PENDING';
    case 'COMPLETED':
      return 'COMPLETED';
    case 'CANCELLED':
    case 'EXPIRED':
      return 'CLOSED';
    case 'IN_PROGRESS':
    case 'DELIVERED':
    case 'DISPUTED':
    default:
      return 'IN_PROGRESS';
  }
}

/** 任务类型 → 中文文案。 */
export function orderTypeLabel(type: OrderType): string {
  switch (type) {
    case 'EXPRESS':
      return '快递代取';
    case 'BUY':
      return '代买';
    case 'SEND':
      return '代送';
    default:
      return type;
  }
}

/** 动作码 → 按钮文案。 */
export function actionLabel(action: ActionCode): string {
  switch (action) {
    case 'ACCEPT':
      return '抢单';
    case 'PICKED_UP':
      return '已取件';
    case 'ON_THE_WAY':
      return '配送中';
    case 'DELIVER':
      return '已送达';
    case 'CONFIRM':
      return '确认收货';
    default:
      return action;
  }
}

/** 期望时长 → 文案。 */
export function expectMinutesLabel(minutes: number | undefined): string {
  switch (minutes) {
    case 30:
      return '30 分钟';
    case 60:
      return '1 小时';
    case 120:
      return '2 小时';
    case 720:
      return '今日内';
    case undefined:
      return '不限';
    default:
      return `${minutes} 分钟`;
  }
}

/**
 * 真实后端的详情接口若没下发 `timeline`，用状态反推一份 6 节点时间轴。
 * 保证详情页在「后端字段未补齐」时也不会白屏。
 */
export function fallbackTimeline(status: OrderStatus): TimelineNode[] {
  const order = ['CREATE', 'ACCEPT', 'PICKED_UP', 'ON_THE_WAY', 'DELIVER', 'CONFIRM'];
  const labels: Record<string, string> = {
    CREATE: '发单',
    ACCEPT: '接单',
    PICKED_UP: '已取件',
    ON_THE_WAY: '配送中',
    DELIVER: '已送达',
    CONFIRM: '完成',
  };

  let doneCount = 1;
  if (status === 'IN_PROGRESS') {
    doneCount = 2;
  } else if (status === 'DELIVERED') {
    doneCount = 5;
  } else if (status === 'COMPLETED') {
    doneCount = 6;
  } else if (status === 'CANCELLED' || status === 'EXPIRED' || status === 'DISPUTED') {
    doneCount = 1;
  }

  return order.map((key, index) => ({
    key,
    label: labels[key] ?? key,
    at: null,
    done: index < doneCount,
    current: index === doneCount - 1 && status !== 'COMPLETED',
  }));
}
