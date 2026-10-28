/**
 * 可执行动作的收敛逻辑（纯函数，无 I/O）。
 *
 * ## 为什么需要这一层
 *
 * 后端 `available_actions[]` **目前只做角色层过滤**（`queries.py::detail_actions`），
 * 不跑状态机守卫。实测样例：接单人已 `PICKED_UP` 时，数组里仍然同时有
 * `PICKED_UP` / `ON_THE_WAY` / `DELIVER`，还夹着 `NOTE` / `REQUEST_CANCEL` 这类
 * 不属于详情页主按钮的动作码。若直接遍历渲染：
 *   1. 会冒出「APPROVE_CANCEL」这种英文按钮（`actionLabel` 兜底返回原始码）；
 *   2. 会出现「点了必然 42201」的按钮。
 *
 * 所以前端做两道收敛（等后端把 `now=` 守卫层接上后，这两道依然无害）：
 *   1. **白名单**：只认 ACCEPT / PICKED_UP / ON_THE_WAY / DELIVER / CONFIRM；
 *   2. **时间轴收敛**：按 `timeline` 最后一个 `done` 节点算出「唯一的下一个合法节点」，
 *      再与白名单取交集 —— 保证同一时刻最多一个主按钮，且点了必成功。
 */

import type { ActionCode, OrderDetail, TimelineNode } from '../types';

/** 详情页主按钮支持的 5 个动作码。 */
const KNOWN_ACTIONS: readonly ActionCode[] = [
  'ACCEPT',
  'PICKED_UP',
  'ON_THE_WAY',
  'DELIVER',
  'CONFIRM',
];

/** 类型守卫：把后端的 `string` 收窄成我们认识的 `ActionCode`。 */
function isKnownAction(value: string): value is ActionCode {
  return (KNOWN_ACTIONS as readonly string[]).includes(value);
}

/** 时间轴里某个节点是否已完成。 */
function isDone(timeline: TimelineNode[], key: string): boolean {
  return timeline.some((node) => node.key === key && node.done);
}

/**
 * 按「状态 + 时间轴」推导唯一的下一个合法动作（严格顺序推进，同 T5 `target == current + 1`）。
 * 终态或缺信息时返回 null。
 */
export function nextActionOf(order: OrderDetail, timeline: TimelineNode[]): ActionCode | null {
  switch (order.status) {
    case 'PENDING':
      return 'ACCEPT';
    case 'IN_PROGRESS': {
      if (!isDone(timeline, 'PICKED_UP')) {
        return 'PICKED_UP';
      }
      if (!isDone(timeline, 'ON_THE_WAY')) {
        return 'ON_THE_WAY';
      }
      return 'DELIVER';
    }
    case 'DELIVERED':
      return 'CONFIRM';
    // COMPLETED / CANCELLED / EXPIRED / DISPUTED：终态或挂起态，没有主按钮
    default:
      return null;
  }
}

/**
 * 计算详情页实际要渲染的按钮（0 或 1 个）。
 *
 * @param order 订单详情
 * @param timeline 已归一化的时间轴（页面层会把后端 `timeline` 或兜底时间轴传进来）
 */
export function resolveOrderActions(order: OrderDetail, timeline: TimelineNode[]): ActionCode[] {
  // 1) 白名单过滤：不认识的动作码一律不渲染（NOTE / URGE / REQUEST_CANCEL ... 不属于主按钮）
  const allowed = (order.available_actions ?? []).filter(isKnownAction);

  // 2) 后端既然一个都不给（例如发单人在进行中、或终态），我们也不该自己造按钮
  if (allowed.length === 0) {
    return [];
  }

  // 3) 用时间轴收敛出唯一的下一个合法节点，再与白名单取交集
  const next = nextActionOf(order, timeline);
  if (next === null) {
    return [];
  }
  return allowed.includes(next) ? [next] : [];
}
