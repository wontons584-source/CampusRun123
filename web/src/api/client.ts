/**
 * API 客户端 —— 前端唯一的网络出口。
 *
 * 一个开关切换两套实现：
 * - `VITE_USE_MOCK=true`  → 调 `mock.ts` 的内存假数据（后端没就绪也能完整演示）
 * - `VITE_USE_MOCK=false` → 调真实后端（统一响应 `{code,data,message,trace_id}`）
 *
 * 所有方法都把「非 0 code」翻译成 `ApiError` 抛出，页面只负责 catch + toast，
 * 这样 mock 与真实接口的错误口径完全一致。
 */

import { ApiError, isNotFoundError } from './errors';
import { getToken, clearToken } from './token';
import {
  mockConfirm,
  mockCreateOrder,
  mockGrabOrder,
  mockHall,
  mockLogin,
  mockMe,
  mockMyOrders,
  mockOrderDetail,
  mockPois,
  mockProgress,
  mockRegister,
} from './mock';
import type {
  ApiResponse,
  AuthResult,
  CreateOrderPayload,
  CreateOrderResult,
  HallOrder,
  OrderDetail,
  Poi,
  ProgressAction,
  User,
} from '../types';

/** 是否走 mock（默认 true：连 .env 都没配时也能直接 `npm run dev` 看界面）。 */
export const isMockMode: boolean = (import.meta.env.VITE_USE_MOCK ?? 'true') !== 'false';

/** 接口基址，留空表示同源（开发期由 vite proxy 转发到 127.0.0.1:8000）。 */
const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '';

const API_PREFIX = '/api/v1';

type HttpMethod = 'GET' | 'POST';

/** 真实网络请求：解包统一响应 `{code,data,message,trace_id}`。 */
async function http<T>(path: string, method: HttpMethod, body?: unknown): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }
  if (token !== null) {
    headers.Authorization = `Bearer ${token}`;
  }

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${API_PREFIX}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError('网络异常，请确认后端服务已启动', -1);
  }

  let envelope: ApiResponse<T> | null = null;
  try {
    envelope = (await response.json()) as ApiResponse<T>;
  } catch {
    envelope = null;
  }

  if (envelope === null) {
    throw new ApiError(`请求失败（HTTP ${response.status}）`, response.status);
  }
  if (envelope.code !== 0) {
    // 鉴权类错误：清掉本地 token，让路由守卫把人送回登录页
    if (envelope.code >= 40100 && envelope.code < 40200) {
      clearToken();
    }
    throw new ApiError(envelope.message || '请求失败', envelope.code);
  }
  return envelope.data;
}

/** mock / 真实二选一，保持调用点清爽。 */
function route<T>(mockFn: () => Promise<T>, realFn: () => Promise<T>): Promise<T> {
  return isMockMode ? mockFn() : realFn();
}

/** 后端返回的 `{order:{...}}` / `{id,...}` 两种形态都兼容。 */
function unwrapCreateResult(raw: CreateOrderResult | { order: CreateOrderResult }): CreateOrderResult {
  if (typeof raw === 'object' && raw !== null && 'order' in raw) {
    return raw.order;
  }
  return raw as CreateOrderResult;
}

/** 详情兜底：真实后端的字段可能还没补齐，这里统一补默认值。 */
function normalizeDetail(raw: OrderDetail): OrderDetail {
  return {
    ...raw,
    description: raw.description ?? null,
    images: raw.images ?? [],
    from_poi_name: raw.from_poi_name ?? '未知地点',
    to_poi_name: raw.to_poi_name ?? '未知地点',
    available_actions: raw.available_actions ?? [],
    timeline: raw.timeline ?? [],
    runner_id: raw.runner_id ?? null,
  };
}

/**
 * 列表信封：后端 `hall` / `mine` 都返回 `{items, total}`（见 `orders.py` 的 `ok({...})`），
 * 不是裸数组。前端统一在 `data.items` 取值；同时兼容裸数组形态，避免后端某天改回来就白屏。
 */
interface ListEnvelope<T> {
  items: T[];
  total: number;
}

/** 取列表：优先 `data.items`，退化为裸数组。 */
function unwrapList<T>(raw: ListEnvelope<T> | T[]): T[] {
  if (Array.isArray(raw)) {
    return raw;
  }
  return Array.isArray(raw.items) ? raw.items : [];
}

/**
 * docs 6.1 版「我的订单」：`GET /me/orders?role=publisher|runner`。
 *
 * `role` 是二选一，所以拉两次再合并；同 id 去重，防止极端情况下两边都返回同一单。
 * 只取第一页（前端不翻页），`next_cursor` 忽略。
 */
async function fetchMyOrdersByRole(): Promise<HallOrder[]> {
  const [asPublisher, asRunner] = await Promise.all([
    http<ListEnvelope<HallOrder> | HallOrder[]>('/me/orders?role=publisher', 'GET'),
    http<ListEnvelope<HallOrder> | HallOrder[]>('/me/orders?role=runner', 'GET'),
  ]);

  const merged = new Map<number, HallOrder>();
  unwrapList(asPublisher).forEach((order) => {
    merged.set(order.id, { ...order, my_role: 'PUBLISHER' });
  });
  unwrapList(asRunner).forEach((order) => {
    if (!merged.has(order.id)) {
      merged.set(order.id, { ...order, my_role: 'RUNNER' });
    }
  });
  return Array.from(merged.values());
}

export const api = {
  /** 注册。 */
  register(input: { nickname: string; phone: string; password: string }): Promise<AuthResult> {
    return route(
      () => mockRegister(input),
      () => http<AuthResult>('/auth/register', 'POST', input),
    );
  },

  /** 登录。 */
  login(input: { phone: string; password: string }): Promise<AuthResult> {
    return route(
      () => mockLogin(input),
      () => http<AuthResult>('/auth/login', 'POST', input),
    );
  },

  /** 当前用户。 */
  me(): Promise<User> {
    return route(
      () => mockMe(),
      () => http<User>('/me', 'GET'),
    );
  },

  /** 地点字典。 */
  pois(): Promise<Poi[]> {
    return route(
      () => mockPois(),
      () => http<Poi[]>('/pois', 'GET'),
    );
  },

  /** 发单。 */
  createOrder(payload: CreateOrderPayload): Promise<CreateOrderResult> {
    return route(
      () => mockCreateOrder(payload),
      async () => unwrapCreateResult(await http<CreateOrderResult | { order: CreateOrderResult }>(
        '/orders',
        'POST',
        payload,
      )),
    );
  },

  /** 抢单大厅（真实接口 `data` 是 `{items,total}`）。 */
  hall(): Promise<HallOrder[]> {
    return route(
      () => mockHall(),
      async () => unwrapList(await http<ListEnvelope<HallOrder> | HallOrder[]>('/orders/hall', 'GET')),
    );
  },

  /**
   * 我的订单（我发的 + 我接的）。
   *
   * ⚠️ 这条接口目前有**两版说法**，后端还没最终落地，前端做了双路径降级：
   *  A) `GET /orders/mine` → `{items,total}`（team-lead 同步的后端新增接口，与 hall 同构）
   *  B) `GET /me/orders?role=publisher|runner` → `{items,next_cursor,has_more}`（docs 6.1，游标分页）
   * 先打 A；A 返回 404/404xx 时退到 B，并按 role 各拉一次合并、顺带打上 `my_role`。
   * 这样无论后端最终落哪一版，UI 都不用改。
   */
  myOrders(): Promise<HallOrder[]> {
    return route(
      () => mockMyOrders(),
      async () => {
        try {
          return unwrapList(
            await http<ListEnvelope<HallOrder> | HallOrder[]>('/orders/mine', 'GET'),
          );
        } catch (error) {
          if (!isNotFoundError(error)) {
            throw error;
          }
          return fetchMyOrdersByRole();
        }
      },
    );
  },

  /** 订单详情。 */
  orderDetail(id: number): Promise<OrderDetail> {
    return route(
      () => mockOrderDetail(id),
      async () => normalizeDetail(await http<OrderDetail>(`/orders/${id}`, 'GET')),
    );
  },

  /** 抢单。 */
  grabOrder(id: number): Promise<OrderDetail> {
    return route(
      () => mockGrabOrder(id),
      async () => normalizeDetail(await http<OrderDetail>(`/orders/${id}/grab`, 'POST', {})),
    );
  },

  /** 上报进度（PICKED_UP / ON_THE_WAY / DELIVER）。 */
  progress(id: number, action: ProgressAction): Promise<OrderDetail> {
    return route(
      () => mockProgress(id, action),
      async () => normalizeDetail(
        await http<OrderDetail>(`/orders/${id}/progress`, 'POST', { action }),
      ),
    );
  },

  /** 确认收货。 */
  confirm(id: number): Promise<OrderDetail> {
    return route(
      () => mockConfirm(id),
      async () => normalizeDetail(await http<OrderDetail>(`/orders/${id}/confirm`, 'POST', {})),
    );
  },
};

export type Api = typeof api;
