"""统一错误模型与错误码常量（T-04, K-02）。

**错误码常量只在本文件定义**（K-02）。前端按 `code` 分支，不按 `message` 分支 ——
所以 message 可以改文案、可以国际化，code 一旦发出去就是契约。

## 分段规则（K-02）

| 段 | 含义 | 典型 HTTP |
|---|---|---|
| `0` | 成功 | 200 |
| `400xx` | 参数非法 / 请求不完整 | 400 |
| `401xx` | 未登录 / 令牌失效 | 401 |
| `403xx` | 已登录但没权限（未认证学号、被封禁、非本人） | 403 |
| `404xx` | 资源不存在 | 404 |
| `409xx` | 冲突（含并发抢占、状态被他人改变） | 409 |
| `410xx` | 已失效（过期） | 410 |
| `422xx` | 状态机非法迁移 / 业务规则不满足 | 422 |
| `429xx` | 频控 / 上限 | 429 |
| `500xx` | 系统与依赖故障 | 500 / 503 |

分段是**语义分层不是分页**：`42902`（发单待接单超限）与 `42903`（催单超限）
都属于「你被上限挡住了」，但前端要给出不同文案，所以必须是两个码。

## 为什么不直接用 HTTP 状态码

抢单失败有 4 种都可以是 409（已被抢 / 在途超限 / 状态不可抢 / 幂等键冲突），
前端要显示 4 句不同的话。只靠 HTTP 状态码只能显示「操作失败」，
用户不知道是该刷新还是该先去完成手上的单。
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """业务异常：携带 HTTP 状态码 + 业务码 + 用户可读文案。

    用法：
        raise ApiError(409, ErrCode.ORDER_ALREADY_TAKEN, "手慢了，订单已被接走")

    `data` 用于给出结构化补充信息（如 `available_actions`、字段级错误列表），
    默认 None；**不要把堆栈、SQL、内部 id 放进 data**。
    """

    def __init__(
        self,
        http_status: int,
        code: int,
        message: str,
        *,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.http_status: int = http_status
        self.code: int = code
        self.message: str = message
        self.data: Any = data

    def to_dict(self) -> dict[str, Any]:
        """供异常处理器复用，不含 trace_id（由 response 层补充）。"""
        return {"code": self.code, "data": self.data, "message": self.message}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ApiError(http_status={self.http_status}, code={self.code}, message={self.message!r})"


class ErrCode:
    """错误码常量表（唯一来源，K-02）。

    命名规则：`<域>_<语义>`，与文档 6.3 的错误码表一一对应。
    """

    # ---- 0 成功 ----
    SUCCESS = 0

    # ---- 400xx 参数 ----
    BAD_REQUEST = 40000
    MISSING_IDEMPOTENCY_KEY = 40001
    PICK_CODE_REQUIRED = 40002          # FR-ORDER-10：EXPRESS 必填取件码
    REWARD_OUT_OF_RANGE = 40003         # BR-07：2..50，.5 步进
    INVALID_ARGUMENT = 40009

    # ---- 401xx 登录态 ----
    UNAUTHENTICATED = 40101
    TOKEN_EXPIRED = 40102
    TOKEN_INVALID = 40103

    # ---- 403xx 权限 / 认证 ----
    NOT_VERIFIED = 40301                # BR-01/D1：未通过学号认证
    ACCOUNT_BANNED = 40302              # US-21：账号被封禁
    CANNOT_GRAB_OWN_ORDER = 40303       # 不能接自己发布的单
    ORDER_NOT_YOURS = 40310             # 非本人：不是该单的相关方

    # ---- 404xx 不存在 ----
    NOT_FOUND = 40400
    ORDER_NOT_FOUND = 40401
    USER_NOT_FOUND = 40402
    POI_NOT_FOUND = 40403

    # ---- 409xx 冲突（含并发） ----
    RESOURCE_CONFLICT = 40900           # 通用唯一冲突（能按约束名细分的走下面几个）
    ORDER_ALREADY_TAKEN = 40901         # BR-17/D7：已被接走（CAS 0 行 / Lua 锁存在）
    INFLIGHT_LIMIT_REACHED = 40902      # BR-16/D8：在途已满 3 单
    ORDER_STATUS_NOT_GRABBABLE = 40903  # BR-17：非 PENDING
    IDEMPOTENCY_KEY_CONFLICT = 40904    # FR-GRAB-08：幂等键被别的订单用过
    STATUS_CHANGED_CONCURRENTLY = 40905 # 状态机 CAS 失败：状态已被其他操作改变
    REVIEW_ALREADY_EXISTS = 40906       # BR-27：一单一评价人一次
    DISPUTE_ALREADY_EXISTS = 40907      # BR-28：一单仅 1 次纠纷
    CANCEL_REQUEST_EXISTS = 40908       # 已有待响应的协商取消申请
    ORDER_NO_CONFLICT = 40909           # order_no 唯一冲突（CR+yyMMdd+6 撞号，重试即可）

    # ---- 410xx 已失效 ----
    ORDER_EXPIRED = 41001               # T4：deadline 已过
    REVIEW_WINDOW_CLOSED = 41002        # BR-24：7 天评价窗口已关
    DISPUTE_WINDOW_CLOSED = 41003       # BR-28：48h 投诉窗口已关

    # ---- 422xx 状态机 / 业务规则 ----
    ILLEGAL_TRANSITION = 42201          # D3：状态机非法迁移或守卫不满足
    PRE_DISPUTE_STATUS_MISSING = 42202  # BR-34：挂起态缺失，无法回滚

    # ---- 429xx 频控 / 上限 ----
    GRAB_TOO_FREQUENT = 42901           # FR-GRAB-08：1s 内 >1 次
    PENDING_LIMIT_REACHED = 42902       # BR-11：同时待接单 > 3
    URGE_LIMIT_REACHED = 42903          # FR-FULFILL-05：每单最多 2 次
    SEND_CODE_TOO_FREQUENT = 42904      # FR-AUTH-05：验证码频控

    # ---- 500xx 系统 ----
    INTERNAL_ERROR = 50000
    DEPENDENCY_UNAVAILABLE = 50300      # 依赖（DB/Redis/存储）不可用


# 常用错误码 → 默认 HTTP 状态。处理器只需 `ApiError(...)` 显式给状态，
# 这张表用于「只给 code 也想拿到合适状态码」的场景（例如后台任务抛错）。
CODE_TO_HTTP_STATUS: dict[int, int] = {
    ErrCode.SUCCESS: 200,
    ErrCode.BAD_REQUEST: 400,
    ErrCode.MISSING_IDEMPOTENCY_KEY: 400,
    ErrCode.PICK_CODE_REQUIRED: 400,
    ErrCode.REWARD_OUT_OF_RANGE: 400,
    ErrCode.INVALID_ARGUMENT: 400,
    ErrCode.UNAUTHENTICATED: 401,
    ErrCode.TOKEN_EXPIRED: 401,
    ErrCode.TOKEN_INVALID: 401,
    ErrCode.NOT_VERIFIED: 403,
    ErrCode.ACCOUNT_BANNED: 403,
    ErrCode.CANNOT_GRAB_OWN_ORDER: 403,
    ErrCode.ORDER_NOT_YOURS: 403,
    ErrCode.NOT_FOUND: 404,
    ErrCode.ORDER_NOT_FOUND: 404,
    ErrCode.USER_NOT_FOUND: 404,
    ErrCode.POI_NOT_FOUND: 404,
    ErrCode.ORDER_ALREADY_TAKEN: 409,
    ErrCode.RESOURCE_CONFLICT: 409,
    ErrCode.ORDER_NO_CONFLICT: 409,
    ErrCode.INFLIGHT_LIMIT_REACHED: 409,
    ErrCode.ORDER_STATUS_NOT_GRABBABLE: 409,
    ErrCode.IDEMPOTENCY_KEY_CONFLICT: 409,
    ErrCode.STATUS_CHANGED_CONCURRENTLY: 409,
    ErrCode.REVIEW_ALREADY_EXISTS: 409,
    ErrCode.DISPUTE_ALREADY_EXISTS: 409,
    ErrCode.CANCEL_REQUEST_EXISTS: 409,
    ErrCode.ORDER_EXPIRED: 410,
    ErrCode.REVIEW_WINDOW_CLOSED: 410,
    ErrCode.DISPUTE_WINDOW_CLOSED: 410,
    ErrCode.ILLEGAL_TRANSITION: 422,
    ErrCode.PRE_DISPUTE_STATUS_MISSING: 422,
    ErrCode.GRAB_TOO_FREQUENT: 429,
    ErrCode.PENDING_LIMIT_REACHED: 429,
    ErrCode.URGE_LIMIT_REACHED: 429,
    ErrCode.SEND_CODE_TOO_FREQUENT: 429,
    ErrCode.INTERNAL_ERROR: 500,
    ErrCode.DEPENDENCY_UNAVAILABLE: 503,
}


def http_status_for(code: int) -> int:
    """按 code 反查 HTTP 状态；未知码归 400（不要默认 500，那会把用户错误报成系统故障）。"""
    return CODE_TO_HTTP_STATUS.get(code, 400)


def raise_api_error(code: int, message: str, *, data: Any = None) -> None:
    """用分段规则推断 HTTP 状态的便捷抛错入口。"""
    raise ApiError(http_status_for(code), code, message, data=data)


# ---------------------------------------------------------------------------
# 常用语义的快捷构造器：避免每个 service 都手写 (status, code) 组合写错。
# ---------------------------------------------------------------------------
def illegal_transition(message: str = "当前状态不允许该操作") -> ApiError:
    """D3：状态机拒绝。**唯一**用于 `Transitions.apply()` 的守卫失败路径。"""
    return ApiError(422, ErrCode.ILLEGAL_TRANSITION, message)


def not_yours(message: str = "无权操作他人的订单") -> ApiError:
    return ApiError(403, ErrCode.ORDER_NOT_YOURS, message)


def conflict(code: int, message: str) -> ApiError:
    return ApiError(http_status_for(code), code, message)


def dependency_down(component: str = "依赖服务") -> ApiError:
    return ApiError(503, ErrCode.DEPENDENCY_UNAVAILABLE, f"{component}暂时不可用，请稍后重试")
