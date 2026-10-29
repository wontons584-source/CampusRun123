/**
 * 详情页可执行动作的白名单过滤（纯函数，无 I/O）。
 *
 * 后端 `available_actions[]` 已经做**守卫层精度**（`queries.py::detail_actions`
 * 传 `now=` + `viewer_id`），只返回「点了真的会成功」的动作——比如已取件后
 * 不会再返回 `PICKED_UP`、跳级动作也不会出现。
 *
 * 但它仍会夹带 `NOTE` / `URGE` / `REQUEST_CANCEL` 这类不属于详情页主按钮的动作码，
 * 直接遍历渲染会冒出「APPROVE_CANCEL」这种英文按钮。所以前端只做一道白名单过滤：
 * 只认 5 个主按钮动作码，其余一律不渲染。
 *
 * 「哪个动作合法」以后端为唯一真相，前端不再自己按时间轴推导。
 */

import type { ActionCode, OrderDetail } from '../types';

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

/**
 * 计算详情页实际要渲染的按钮。
 *
 * 后端 `available_actions` 已是守卫层精度（每个主按钮点了都会成功），
 * 前端只把非主按钮的动作码（NOTE / URGE / REQUEST_CANCEL …）滤掉。
 */
export function resolveOrderActions(order: OrderDetail): ActionCode[] {
  return (order.available_actions ?? []).filter(isKnownAction);
}
