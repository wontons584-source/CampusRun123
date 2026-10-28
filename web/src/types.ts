/**
 * 全局领域类型 —— 与后端 `docs` 契约逐字对齐。
 *
 * 约定：
 * - `Money` 用 `number | string`：后端 `Decimal` 序列化成字符串（避免 JS 浮点误差），
 *   但 mock 与部分接口会直接给 number，前端统一用 `toNumber()` 归一化后再参与计算。
 * - 所有可选字段（`?`）都是「真实后端可能暂时没实现」的字段，UI 必须给兜底值。
 */

/** 任务类型（FR-ORDER-01）。 */
export type OrderType = 'EXPRESS' | 'BUY' | 'SEND';

/** 订单 7 状态（5.4）。`DISPUTED` 是挂起态，非终态。 */
export type OrderStatus =
  | 'PENDING'
  | 'IN_PROGRESS'
  | 'DELIVERED'
  | 'COMPLETED'
  | 'CANCELLED'
  | 'EXPIRED'
  | 'DISPUTED';

/** 后端下发的可执行动作码。 */
export type ActionCode = 'ACCEPT' | 'PICKED_UP' | 'ON_THE_WAY' | 'DELIVER' | 'CONFIRM';

/** 金额：后端 Decimal → string，mock → number。 */
export type Money = number | string;

/** 统一响应包装：`code === 0` 才是成功。 */
export interface ApiResponse<T> {
  code: number;
  data: T;
  message: string;
  trace_id: string;
}

/** 用户。 */
export interface User {
  id: number;
  nickname: string;
  is_runner_mode: boolean;
  phone?: string;
}

/** 地点字典项（BR-08：起点/终点必须来自字典）。 */
export interface Poi {
  id: number;
  name: string;
  type: string;
  lat: number;
  lng: number;
}

/** 登录/注册返回。 */
export interface AuthResult {
  token: string;
  user: User;
}

/** 大厅卡片（GET /orders/hall 的元素）。 */
export interface HallOrder {
  id: number;
  order_no: string;
  status: OrderStatus;
  reward: Money;
  /** ISO 8601，含 Z */
  deadline: string;
  from_poi_name: string;
  to_poi_name: string;
  available_actions: ActionCode[];
  /** 我在本单里的角色（仅 `/orders/mine` 下发，权威值优先于 publisher_id 推断） */
  my_role?: 'PUBLISHER' | 'RUNNER';
  /** 以下字段真实后端可能暂未下发 */
  type?: OrderType;
  description?: string | null;
  created_at?: string;
  publisher_id?: number;
  publisher_name?: string;
  runner_id?: number | null;
  runner_name?: string | null;
}

/** 时间轴节点（详情页竖直时间轴渲染单元）。 */
export interface TimelineNode {
  /** 与动作码同名：CREATE / ACCEPT / PICKED_UP / ON_THE_WAY / DELIVER / CONFIRM */
  key: string;
  label: string;
  /** null 表示尚未发生 */
  at: string | null;
  done: boolean;
  current: boolean;
}

/** 订单详情（GET /orders/{id}）。 */
export interface OrderDetail {
  id: number;
  order_no: string;
  status: OrderStatus;
  type: OrderType;
  reward: Money;
  deadline: string;
  from_poi_id: number;
  to_poi_id: number;
  from_poi_name: string;
  to_poi_name: string;
  description?: string | null;
  images?: string[];
  /** 取件码：仅发单人在详情页可见（后端密文不外泄给跑腿者） */
  pick_code?: string | null;
  expect_minutes?: number;
  created_at?: string;
  publisher_id: number;
  publisher_name?: string;
  runner_id?: number | null;
  runner_name?: string | null;
  available_actions: ActionCode[];
  timeline?: TimelineNode[];
}

/** 发单入参（POST /orders）。 */
export interface CreateOrderPayload {
  type: OrderType;
  from_poi_id: number;
  to_poi_id: number;
  /** 0.5 步进，2–50 */
  reward: number;
  /** 30 / 60 / 120 / 720(今日内) */
  expect_minutes: 30 | 60 | 120 | 720;
  description?: string;
  images?: string[];
  /** type === 'EXPRESS' 时必填，4–20 位 */
  pick_code?: string;
}

/** 发单返回（POST /orders → data）。 */
export interface CreateOrderResult {
  id: number;
  order_no: string;
  status: OrderStatus;
}

/** 进度上报动作（POST /orders/{id}/progress）。 */
export type ProgressAction = 'PICKED_UP' | 'ON_THE_WAY' | 'DELIVER';
