-- grab.lua —— 抢单锁（T-05 占位实现；真正的原子抢占在 T-20）
--
-- KEYS[1] = grab:order:{order_id}
-- ARGV[1] = token（锁持有者标识，释放时必须校验，避免误删别人的锁）
-- ARGV[2] = ttl_ms
--
-- 返回 1 = 拿到锁；0 = 已被别人持有
--
-- 为什么用 SET NX PX 而不是 SETNX + EXPIRE：
-- 两条命令之间进程崩掉会留下**永不过期的锁**，那一单就永远抢不了（只能人工删键）。
-- SET NX PX 是一条命令，TTL 与值同生共死。

local ok = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', tonumber(ARGV[2]))
if ok then
    return 1
end
return 0
