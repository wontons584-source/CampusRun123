-- quota.lua —— 原子频控计数（T-05 占位实现；推送频控在 T-18）
--
-- KEYS[1] = pushq:{user_id}:{window}
-- ARGV[1] = ttl_s
--
-- 返回当前计数（INCR 后的值）
--
-- 为什么必须原子：`INCR` 与 `EXPIRE` 是两条命令，中间崩溃会留下一个
-- **永不过期的计数器** —— 该用户之后永远被限流，且没有任何报错。
-- 只在 count == 1 时设 TTL：每次请求都 EXPIRE 会让窗口不断后移，
-- 变成"只要一直有新消息，计数永不清零"。

local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return count
