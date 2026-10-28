/**
 * Mock 后端（内存 + localStorage 兜底持久化）。
 *
 * 目的：后端接口没就绪时，界面能**真实走完**「抢单 → 取件 → 配送 → 送达 → 确认」
 * 的完整状态流转，用于录屏与前端自测。
 *
 * 设计要点：
 * 1. 状态就是一张 `MockDb`（users / pois / orders），每次变更 `saveDb()`，
 *    刷新页面后数据还在——录屏中途刷新不会丢订单。
 * 2. 状态机口径与后端 `state_machine.py` 保持一致：
 *    PENDING → (ACCEPT) → IN_PROGRESS → (PICKED_UP/ON_THE_WAY) → IN_PROGRESS
 *    → (DELIVER) → DELIVERED → (CONFIRM) → COMPLETED
 * 3. `available_actions` 由「当前登录用户 + 订单角色 + 已发生的进度事实」推导，
 *    与真实后端同源逻辑，切换真实接口后 UI 不需要改。
 */

import { ApiError } from './errors';
import { getToken } from './token';
import type {
  ActionCode,
  AuthResult,
  CreateOrderPayload,
  CreateOrderResult,
  HallOrder,
  OrderDetail,
  OrderStatus,
  OrderType,
  Poi,
  ProgressAction,
  TimelineNode,
  User,
} from '../types';

/** 模拟网络延迟，让加载态/骨架屏在录屏时可见但不拖沓。 */
const MOCK_LATENCY_MS = 160;

/** 存储 key 带版本号，结构变更时自动丢弃旧数据。 */
const STORAGE_KEY = 'campusrun.mock.v1';

const DB_VERSION = 1;

interface MockUser extends User {
  phone: string;
  password: string;
}

interface MockEvent {
  action: string;
  at: string;
  actor_id: number;
}

type ExpectMinutes = 30 | 60 | 120 | 720;

interface MockOrder {
  id: number;
  order_no: string;
  type: OrderType;
  status: OrderStatus;
  reward: number;
  deadline: string;
  from_poi_id: number;
  to_poi_id: number;
  description: string | null;
  images: string[];
  pick_code: string | null;
  expect_minutes: ExpectMinutes;
  created_at: string;
  publisher_id: number;
  runner_id: number | null;
  events: MockEvent[];
}

interface MockDb {
  version: number;
  users: MockUser[];
  pois: Poi[];
  orders: MockOrder[];
  next_user_id: number;
  next_order_id: number;
}

const TIMELINE_STEPS: ReadonlyArray<{ key: string; label: string }> = [
  { key: 'CREATE', label: '发单' },
  { key: 'ACCEPT', label: '接单' },
  { key: 'PICKED_UP', label: '已取件' },
  { key: 'ON_THE_WAY', label: '配送中' },
  { key: 'DELIVER', label: '已送达' },
  { key: 'CONFIRM', label: '完成' },
];

const PROGRESS_ACTIONS: ReadonlyArray<ProgressAction> = ['PICKED_UP', 'ON_THE_WAY', 'DELIVER'];

/** 演示账号（录屏时直接用它登录，不用现场注册）。 */
export const MOCK_DEMO_ACCOUNTS: ReadonlyArray<{ phone: string; password: string; label: string }> = [
  { phone: '13800000001', password: '123456', label: '发单同学 · 李小明' },
  { phone: '13800000002', password: '123456', label: '跑腿同学 · 阿杰' },
];

// ---------------------------------------------------------------------------
// 基础工具
// ---------------------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function isoNow(offsetMinutes = 0): string {
  return new Date(Date.now() + offsetMinutes * 60_000).toISOString();
}

function ymd(date: Date): string {
  const y = date.getFullYear();
  const m = `${date.getMonth() + 1}`.padStart(2, '0');
  const d = `${date.getDate()}`.padStart(2, '0');
  return `${y}${m}${d}`;
}

function makeOrderNo(seq: number): string {
  return `CR${ymd(new Date())}${`${seq}`.padStart(6, '0')}`;
}

// ---------------------------------------------------------------------------
// 种子数据
// ---------------------------------------------------------------------------

const SEED_POIS: Poi[] = [
  { id: 1, name: '菜鸟驿站（1号柜）', type: 'EXPRESS', lat: 30.2741, lng: 120.1551 },
  { id: 2, name: '京东快递点', type: 'EXPRESS', lat: 30.2733, lng: 120.1562 },
  { id: 3, name: '南门快递超市', type: 'EXPRESS', lat: 30.2712, lng: 120.1548 },
  { id: 4, name: '第三食堂', type: 'FOOD', lat: 30.2752, lng: 120.1573 },
  { id: 5, name: '图书馆', type: 'BUILDING', lat: 30.2748, lng: 120.1539 },
  { id: 6, name: '6号宿舍楼', type: 'DORM', lat: 30.2761, lng: 120.1586 },
  { id: 7, name: '教学楼A', type: 'BUILDING', lat: 30.2755, lng: 120.1521 },
  { id: 8, name: '体育馆', type: 'SPORT', lat: 30.2778, lng: 120.1595 },
  { id: 9, name: '校医院', type: 'SERVICE', lat: 30.2727, lng: 120.1566 },
  { id: 10, name: '西门', type: 'GATE', lat: 30.2739, lng: 120.1507 },
];

function createSeedDb(): MockDb {
  const publisherId = 2;
  const meId = 1;

  const orders: MockOrder[] = [
    {
      id: 1,
      order_no: makeOrderNo(1),
      type: 'EXPRESS',
      status: 'PENDING',
      reward: 5,
      deadline: isoNow(24),
      from_poi_id: 1,
      to_poi_id: 6,
      description: '一个中号纸箱，麻烦轻放，到楼下打电话。',
      images: [],
      pick_code: '8-2-3045',
      expect_minutes: 30,
      created_at: isoNow(-6),
      publisher_id: publisherId,
      runner_id: null,
      events: [{ action: 'CREATE', at: isoNow(-6), actor_id: publisherId }],
    },
    {
      id: 2,
      order_no: makeOrderNo(2),
      type: 'BUY',
      status: 'PENDING',
      reward: 4.5,
      deadline: isoNow(38),
      from_poi_id: 4,
      to_poi_id: 5,
      description: '三楼窗口的香菇滑鸡饭 + 一杯豆浆，不要辣。',
      images: [],
      pick_code: null,
      expect_minutes: 60,
      created_at: isoNow(-11),
      publisher_id: publisherId,
      runner_id: null,
      events: [{ action: 'CREATE', at: isoNow(-11), actor_id: publisherId }],
    },
    {
      id: 3,
      order_no: makeOrderNo(3),
      type: 'SEND',
      status: 'PENDING',
      reward: 3.5,
      deadline: isoNow(52),
      from_poi_id: 5,
      to_poi_id: 10,
      description: '一份纸质材料，送到西门交给门卫即可。',
      images: [],
      pick_code: null,
      expect_minutes: 120,
      created_at: isoNow(-4),
      publisher_id: publisherId,
      runner_id: null,
      events: [{ action: 'CREATE', at: isoNow(-4), actor_id: publisherId }],
    },
    {
      id: 4,
      order_no: makeOrderNo(4),
      type: 'EXPRESS',
      status: 'PENDING',
      reward: 6,
      deadline: isoNow(16),
      from_poi_id: 2,
      to_poi_id: 7,
      description: '两个小件，取件码在柜机上，尽快。',
      images: [],
      pick_code: 'JD-77120',
      expect_minutes: 30,
      created_at: isoNow(-2),
      publisher_id: publisherId,
      runner_id: null,
      events: [{ action: 'CREATE', at: isoNow(-2), actor_id: publisherId }],
    },
    {
      id: 5,
      order_no: makeOrderNo(5),
      type: 'BUY',
      status: 'PENDING',
      reward: 8,
      deadline: isoNow(95),
      from_poi_id: 9,
      to_poi_id: 6,
      description: '校医院取一盒感冒药（已付款，报手机号后四位 1216）。',
      images: [],
      pick_code: null,
      expect_minutes: 120,
      created_at: isoNow(-3),
      publisher_id: meId,
      runner_id: null,
      events: [{ action: 'CREATE', at: isoNow(-3), actor_id: meId }],
    },
    {
      id: 6,
      order_no: makeOrderNo(6),
      type: 'EXPRESS',
      status: 'COMPLETED',
      reward: 5.5,
      deadline: isoNow(-180),
      from_poi_id: 3,
      to_poi_id: 8,
      description: '一个羽毛球拍，已顺利送到。',
      images: [],
      pick_code: 'SF-9081',
      expect_minutes: 60,
      created_at: isoNow(-260),
      publisher_id: meId,
      runner_id: publisherId,
      events: [
        { action: 'CREATE', at: isoNow(-260), actor_id: meId },
        { action: 'ACCEPT', at: isoNow(-250), actor_id: publisherId },
        { action: 'PICKED_UP', at: isoNow(-240), actor_id: publisherId },
        { action: 'ON_THE_WAY', at: isoNow(-230), actor_id: publisherId },
        { action: 'DELIVER', at: isoNow(-200), actor_id: publisherId },
        { action: 'CONFIRM', at: isoNow(-198), actor_id: meId },
      ],
    },
  ];

  return {
    version: DB_VERSION,
    users: [
      {
        id: meId,
        nickname: '李小明',
        phone: '13800000001',
        password: '123456',
        is_runner_mode: false,
      },
      {
        id: publisherId,
        nickname: '跑腿阿杰',
        phone: '13800000002',
        password: '123456',
        is_runner_mode: true,
      },
    ],
    pois: SEED_POIS,
    orders,
    next_user_id: 3,
    next_order_id: 7,
  };
}

// ---------------------------------------------------------------------------
// 持久化
// ---------------------------------------------------------------------------

function loadDb(): MockDb {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw !== null) {
      const parsed = JSON.parse(raw) as MockDb;
      if (parsed.version === DB_VERSION && Array.isArray(parsed.orders)) {
        return parsed;
      }
    }
  } catch {
    // 解析失败直接落回种子数据
  }
  const fresh = createSeedDb();
  saveDb(fresh);
  return fresh;
}

function saveDb(target: MockDb): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(target));
  } catch {
    // 忽略写入失败（内存态依然可用）
  }
}

let db: MockDb = loadDb();

/** 重置 mock 数据（调试用：控制台 `__campusRunResetMock()` 后刷新）。 */
export function resetMockDb(): void {
  db = createSeedDb();
  saveDb(db);
}

declare global {
  interface Window {
    __campusRunResetMock?: () => void;
  }
}

window.__campusRunResetMock = resetMockDb;

// ---------------------------------------------------------------------------
// 内部工具
// ---------------------------------------------------------------------------

function toPublicUser(user: MockUser): User {
  return {
    id: user.id,
    nickname: user.nickname,
    is_runner_mode: user.is_runner_mode,
    phone: user.phone,
  };
}

function currentUserId(): number {
  const token = getToken();
  if (token === null) {
    throw new ApiError('尚未登录，请先登录', 40101);
  }
  const parts = token.split('.');
  const id = Number(parts[1]);
  if (!Number.isFinite(id) || id <= 0) {
    throw new ApiError('登录状态已失效，请重新登录', 40101);
  }
  const exists = db.users.some((u) => u.id === id);
  if (!exists) {
    throw new ApiError('登录状态已失效，请重新登录', 40101);
  }
  return id;
}

function currentUser(): MockUser {
  const id = currentUserId();
  const user = db.users.find((u) => u.id === id);
  if (user === undefined) {
    throw new ApiError('用户不存在', 40404);
  }
  return user;
}

function findOrder(id: number): MockOrder {
  const order = db.orders.find((o) => o.id === id);
  if (order === undefined) {
    throw new ApiError('订单不存在或已被删除', 40401);
  }
  return order;
}

function poiName(poiId: number): string {
  const poi = db.pois.find((p) => p.id === poiId);
  return poi === undefined ? `地点#${poiId}` : poi.name;
}

function userName(userId: number | null): string | null {
  if (userId === null) {
    return null;
  }
  const user = db.users.find((u) => u.id === userId);
  return user === undefined ? null : user.nickname;
}

function hasEvent(order: MockOrder, action: string): boolean {
  return order.events.some((e) => e.action === action);
}

function pushEvent(order: MockOrder, action: string, actorId: number): void {
  order.events.push({ action, at: new Date().toISOString(), actor_id: actorId });
}

/** 构造 6 节点时间轴：已完成打勾，最后一个已完成节点为当前高亮。 */
function buildTimeline(order: MockOrder): TimelineNode[] {
  const nodes: TimelineNode[] = TIMELINE_STEPS.map((step) => {
    const event = order.events.find((e) => e.action === step.key);
    return {
      key: step.key,
      label: step.label,
      at: event === undefined ? null : event.at,
      done: step.key === 'CREATE' ? true : event !== undefined,
      current: false,
    };
  });

  let lastDoneIndex = -1;
  nodes.forEach((node, index) => {
    if (node.done) {
      lastDoneIndex = index;
    }
  });
  if (lastDoneIndex >= 0) {
    const node = nodes[lastDoneIndex];
    if (node !== undefined) {
      node.current = order.status !== 'COMPLETED';
    }
  }
  return nodes;
}

/** 由「当前用户 + 角色 + 已有进度」推导可执行动作，口径同后端。 */
function deriveActions(order: MockOrder, meId: number): ActionCode[] {
  if (order.status === 'PENDING') {
    return order.publisher_id === meId ? [] : ['ACCEPT'];
  }
  if (order.status === 'IN_PROGRESS') {
    if (order.runner_id !== meId) {
      return [];
    }
    if (!hasEvent(order, 'PICKED_UP')) {
      return ['PICKED_UP'];
    }
    if (!hasEvent(order, 'ON_THE_WAY')) {
      return ['ON_THE_WAY'];
    }
    return ['DELIVER'];
  }
  if (order.status === 'DELIVERED') {
    return order.publisher_id === meId ? ['CONFIRM'] : [];
  }
  return [];
}

function toHallOrder(order: MockOrder): HallOrder {
  return {
    id: order.id,
    order_no: order.order_no,
    status: order.status,
    reward: order.reward,
    deadline: order.deadline,
    from_poi_name: poiName(order.from_poi_id),
    to_poi_name: poiName(order.to_poi_id),
    available_actions: deriveActions(order, currentUserId()),
    type: order.type,
    description: order.description,
    created_at: order.created_at,
    publisher_id: order.publisher_id,
    publisher_name: userName(order.publisher_id) ?? undefined,
    runner_id: order.runner_id,
    runner_name: userName(order.runner_id),
  };
}

function toOrderDetail(order: MockOrder): OrderDetail {
  const meId = currentUserId();
  return {
    id: order.id,
    order_no: order.order_no,
    status: order.status,
    type: order.type,
    reward: order.reward,
    deadline: order.deadline,
    from_poi_id: order.from_poi_id,
    to_poi_id: order.to_poi_id,
    from_poi_name: poiName(order.from_poi_id),
    to_poi_name: poiName(order.to_poi_id),
    description: order.description,
    images: order.images,
    // 取件码只对发单人可见
    // 取件码解锁矩阵（docs 6.1）：发单人始终可见；
    // 接单人从「已接单」起可见（要去取件，必须能看见码）；其余人 null。
    pick_code:
      order.publisher_id === meId ||
      (order.runner_id === meId && order.status !== 'PENDING')
        ? order.pick_code
        : null,
    expect_minutes: order.expect_minutes,
    created_at: order.created_at,
    publisher_id: order.publisher_id,
    publisher_name: userName(order.publisher_id) ?? undefined,
    runner_id: order.runner_id,
    runner_name: userName(order.runner_id),
    available_actions: deriveActions(order, meId),
    timeline: buildTimeline(order),
  };
}

// ---------------------------------------------------------------------------
// 对外 mock 接口（签名与真实 client 完全一致）
// ---------------------------------------------------------------------------

/** 注册。 */
export async function mockRegister(input: {
  nickname: string;
  phone: string;
  password: string;
}): Promise<AuthResult> {
  await sleep(MOCK_LATENCY_MS);
  const nickname = input.nickname.trim();
  const phone = input.phone.trim();
  const password = input.password;

  if (nickname.length < 2 || nickname.length > 16) {
    throw new ApiError('昵称需 2–16 位', 40001);
  }
  if (!/^1\d{10}$/.test(phone)) {
    throw new ApiError('请输入 11 位手机号', 40002);
  }
  if (password.length < 6) {
    throw new ApiError('密码至少 6 位', 40003);
  }
  if (db.users.some((u) => u.phone === phone)) {
    throw new ApiError('该手机号已注册，请直接登录', 40901);
  }

  const user: MockUser = {
    id: db.next_user_id,
    nickname,
    phone,
    password,
    is_runner_mode: false,
  };
  db.next_user_id += 1;
  db.users.push(user);
  saveDb(db);

  return { token: `mock.${user.id}.${Date.now()}`, user: toPublicUser(user) };
}

/** 登录。 */
export async function mockLogin(input: { phone: string; password: string }): Promise<AuthResult> {
  await sleep(MOCK_LATENCY_MS);
  const phone = input.phone.trim();
  const user = db.users.find((u) => u.phone === phone);
  if (user === undefined) {
    throw new ApiError('手机号或密码错误', 40102);
  }
  if (user.password !== input.password) {
    throw new ApiError('手机号或密码错误', 40102);
  }
  return { token: `mock.${user.id}.${Date.now()}`, user: toPublicUser(user) };
}

/** 当前用户。 */
export async function mockMe(): Promise<User> {
  await sleep(MOCK_LATENCY_MS / 2);
  return toPublicUser(currentUser());
}

/** 地点字典。 */
export async function mockPois(): Promise<Poi[]> {
  await sleep(MOCK_LATENCY_MS / 2);
  return [...db.pois];
}

/** 发单。 */
export async function mockCreateOrder(payload: CreateOrderPayload): Promise<CreateOrderResult> {
  await sleep(MOCK_LATENCY_MS);
  const me = currentUser();

  if (payload.from_poi_id === payload.to_poi_id) {
    throw new ApiError('起点和终点不能相同', 40004);
  }
  if (!db.pois.some((p) => p.id === payload.from_poi_id)) {
    throw new ApiError('起点不在可选地点中', 40005);
  }
  if (!db.pois.some((p) => p.id === payload.to_poi_id)) {
    throw new ApiError('终点不在可选地点中', 40005);
  }
  if (payload.reward < 2 || payload.reward > 50) {
    throw new ApiError('酬劳需在 2–50 元之间', 40006);
  }
  if (Math.round(payload.reward * 2) !== payload.reward * 2) {
    throw new ApiError('酬劳需以 0.5 元为步进', 40006);
  }
  const pickCode = payload.pick_code?.trim() ?? '';
  if (payload.type === 'EXPRESS') {
    if (pickCode.length < 4 || pickCode.length > 20) {
      throw new ApiError('取件码需 4–20 位', 40002);
    }
  }

  const id = db.next_order_id;
  db.next_order_id += 1;

  const order: MockOrder = {
    id,
    order_no: makeOrderNo(id),
    type: payload.type,
    status: 'PENDING',
    reward: payload.reward,
    deadline: isoNow(payload.expect_minutes),
    from_poi_id: payload.from_poi_id,
    to_poi_id: payload.to_poi_id,
    description: payload.description?.trim() ? payload.description.trim() : null,
    images: payload.images ?? [],
    pick_code: payload.type === 'EXPRESS' ? pickCode : null,
    expect_minutes: payload.expect_minutes,
    created_at: new Date().toISOString(),
    publisher_id: me.id,
    runner_id: null,
    events: [{ action: 'CREATE', at: new Date().toISOString(), actor_id: me.id }],
  };
  db.orders.push(order);
  saveDb(db);

  return { id: order.id, order_no: order.order_no, status: order.status };
}

/** 抢单大厅（只返回别人发的 PENDING 单）。 */
export async function mockHall(): Promise<HallOrder[]> {
  await sleep(MOCK_LATENCY_MS);
  const meId = currentUserId();
  return db.orders
    .filter((o) => o.status === 'PENDING' && o.publisher_id !== meId)
    .sort((a, b) => a.deadline.localeCompare(b.deadline))
    .map(toHallOrder);
}

/** 我的订单：我发的 + 我接的。 */
/** 我的订单：我发的 + 我接的（带 `my_role`，与真实接口同构）。 */
export async function mockMyOrders(): Promise<HallOrder[]> {
  await sleep(MOCK_LATENCY_MS);
  const meId = currentUserId();
  return db.orders
    .filter((o) => o.publisher_id === meId || o.runner_id === meId)
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((order) => {
      const myRole: 'PUBLISHER' | 'RUNNER' =
        order.publisher_id === meId ? 'PUBLISHER' : 'RUNNER';
      return { ...toHallOrder(order), my_role: myRole };
    });
}

/** 订单详情。 */
export async function mockOrderDetail(id: number): Promise<OrderDetail> {
  await sleep(MOCK_LATENCY_MS);
  currentUserId();
  return toOrderDetail(findOrder(id));
}

/** 抢单。 */
export async function mockGrabOrder(id: number): Promise<OrderDetail> {
  await sleep(MOCK_LATENCY_MS);
  const me = currentUser();
  const order = findOrder(id);

  if (order.status !== 'PENDING') {
    throw new ApiError(
      order.runner_id === null ? '当前状态不可抢单' : '手慢了，这单已被接走',
      order.runner_id === null ? 40903 : 40901,
    );
  }
  if (order.publisher_id === me.id) {
    throw new ApiError('不能接自己发的单', 40303);
  }

  order.status = 'IN_PROGRESS';
  order.runner_id = me.id;
  pushEvent(order, 'ACCEPT', me.id);
  saveDb(db);
  return toOrderDetail(order);
}

/** 进度上报：PICKED_UP / ON_THE_WAY / DELIVER。 */
export async function mockProgress(id: number, action: ProgressAction): Promise<OrderDetail> {
  await sleep(MOCK_LATENCY_MS);
  const me = currentUser();
  const order = findOrder(id);

  if (!PROGRESS_ACTIONS.includes(action)) {
    throw new ApiError('不支持的进度动作', 40008);
  }
  if (order.status !== 'IN_PROGRESS') {
    throw new ApiError('当前订单状态不允许上报进度', 42201);
  }
  if (order.runner_id !== me.id) {
    throw new ApiError('只有接单人可操作该订单', 40301);
  }
  // 严格顺序推进（T5：target == current + 1），跳级一律 42201
  if (!deriveActions(order, me.id).includes(action)) {
    throw new ApiError('请按顺序操作：取件 → 配送中 → 已送达', 42201);
  }

  pushEvent(order, action, me.id);
  if (action === 'DELIVER') {
    order.status = 'DELIVERED';
  }
  saveDb(db);
  return toOrderDetail(order);
}

/** 确认收货。 */
export async function mockConfirm(id: number): Promise<OrderDetail> {
  await sleep(MOCK_LATENCY_MS);
  const me = currentUser();
  const order = findOrder(id);

  if (order.status !== 'DELIVERED') {
    throw new ApiError('订单还未送达，无法确认收货', 42201);
  }
  if (order.publisher_id !== me.id) {
    throw new ApiError('只有发单人可以确认收货', 40310);
  }

  order.status = 'COMPLETED';
  pushEvent(order, 'CONFIRM', me.id);
  saveDb(db);
  return toOrderDetail(order);
}
