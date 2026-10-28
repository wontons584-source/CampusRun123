# CampusRun 课程答辩 PPT — 设计规范（DESIGN.md）

## 全局视觉约定

### ① 画布与安全区
- 画布 1280×720；padding 上下 60px / 左右 60px；内容区 1160×600

### ② 配色方案（科技蓝）
| 槽位 | Hex | 用途 |
|---|---|---|
| 主色 | `#2563EB` | 标题栏、编号、强调图形 |
| 辅色 | `#06B6D4` | 卡片底、分隔线、第二系列 |
| 强调色 | `#F59E0B` | 关键数字、核心结论（每页 ≤3 处） |
| 底色 | `#FFFFFF` | 主背景 |
| 正文色 | `#1E293B` | 主文字 |
| muted | `#64748B` | 次要文字、脚注 |

### ③ 字体系统
- 中文：微软雅黑；西文：Helvetica
- 字号阶梯：封面主标 64 / 页面主标 36 / 卡片小标 22 / 正文 18 / 脚注 14
- 巨型数字 72–96（Bold）

### ④ 视觉母题
标题下 1px 贯通细线（`#E5E7EB`），每页内容页复用。

### ⑤ 配图清单
- 运行效果截图（P14）：占位，录屏后补
- 列宽报错图（P12/13）：占位
- 其余结构化图形用内联 SVG / 大数字

## 18 页逐页字段

| # | title | type | role | rhythm | layout | 备注 |
|---|---|---|---|---|---|---|
| 1 | 封面：CampusRun 校园跑腿 | cover | hero | peak | 大字报式 | 无图、无数据 |
| 2 | 我的出发点 | content | supporting | valley | 左标题色块+右内容 | 为什么做 |
| 3 | 痛点：一个具体的下午 | content | supporting | valley | 左图右文 | 场景故事 |
| 4 | 痛点：四类痛点 | content | supporting | valley | 卡片组+结论条 | 非均分 |
| 5 | 解决方案 | content | supporting | valley | 流程链 | 发单→抢单→送达 |
| 6 | 三条设计原则 | content | supporting | valley | 三栏非均分 | 5:3:2 |
| 7 | 技术架构 | content | supporting | valley | 分层架构图 SVG | 前端/API/领域/数据 |
| 8 | 数据模型 | content | supporting | valley | 巨型数字+洞察 | 14表/27索引/17迁移 |
| 9 | 功能列表 | content | supporting | valley | 三栏对比 | 已完成/补齐/没做 |
| 10 | 难点一：45分钟双出口 | content | supporting | valley | 对比式 | T16/T17 互斥 |
| 11 | 难点二：并发抢单三道闸 | content | hero | peak | 巨型数字+洞察 | 20线程→1赢家 |
| 12 | 难点三：707全绿真库炸 | content | supporting | valley | 三案例表 | 列宽/在途/不落库 |
| 13 | 质量验证：数字与含义 | content | supporting | valley | 数据展示 | 707/14表/三层判据 |
| 14 | 运行效果 | content | supporting | valley | 左图右文 | 截图占位 |
| 15 | 开发方法：和AI配合 | content | supporting | valley | 流程链 | 我出判据它出实现 |
| 16 | 个人收获 | content | supporting | valley | 卡片组+结论条 | |
| 17 | 后续规划 | content | supporting | valley | 左标题色块+右内容 | 学号认证/推送 P0 |
| 18 | 致谢 Q&A | ending | hero | peak | 收束金句+落款 | |

## 反 AI 味纪律
- 禁「卓越」「行业领先」「颠覆」等卖点词
- 禁 emoji、感叹号堆砌
- 数据必须落判断（每个数字后补一句「这说明什么」）
- 未完成的功能如实标注「本次没做」，不混进「已完成」
