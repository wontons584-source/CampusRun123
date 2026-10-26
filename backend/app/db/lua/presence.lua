-- presence.lua —— 心跳（T-05 占位实现；位置心跳在 T-15）
--
-- KEYS[1] = geo:runners:{campus_id}
-- KEYS[2] = runner:alive:{user_id}
-- ARGV[1] = lng
-- ARGV[2] = lat
-- ARGV[3] = user_id（GEO member）
-- ARGV[4] = ttl_s（默认 90）
--
-- 返回 1
--
-- 两件事必须一起做：
-- ① `GEOADD` 把接单人放回在岗集合（关模式/下线时业务侧 `ZREM`）；
-- ② 写 `runner:alive:{uid}` 存活标记 + 给 GEO 集合设 TTL。
-- 只做 ① 会留下"关页面未发请求"的僵尸候选：人已经不在了，但 GEO 里还有他，
-- 定向推送会推给一个永远不响应的人（BR-19 的 TTL 兜底就是为了这个）。

redis.call('GEOADD', KEYS[1], ARGV[1], ARGV[2], ARGV[3])
redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[4]))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return 1
