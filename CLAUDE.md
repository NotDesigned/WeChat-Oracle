# Project notes for Claude

## 用户偏好

- **先想后动**：动手前先把意图、权衡、影响范围想清楚再写代码。
- **简单但不简陋**：选最直接的实现，能用一个函数说清楚的事不要拆三层。
- **不堆错误处理 / 冗余函数**：除非确有必要或用户明说，不要预防性地加 try/except、参数校验、辅助包装。健壮性靠数据契约和显式失败，不靠到处兜底。
- **改动有取舍时先问用户**：涉及策略选择（路径布局、复制 vs 引用、是否删数据等），列出 trade-offs 让用户拍板，不擅自决定。
- **destructive 操作必须确认**：删 DB 行、清目录、`git reset --hard` 之类，先讲清后果再执行。

## 项目特性约定

- **数据本地优先**：`data/` 是项目自有归档，导入时把媒体复制进来（`data/media/<group_id>/<kind>/`），不留对外部路径的依赖。
- **跨源去重**：所有写入路径走 `write_messages()` → `UNIQUE(dedupe_key)`，新增 importer 时复用、不要绕过。
- **`source` 字段记录管道来源**（`live` / `backfill`），不要用它表达消息状态——状态走 `status` 列。
- **WeFlow 是唯一真相源**：实时抓和历史回灌都过 WeFlow，不直接读微信原始 DB。

## 管道字段对齐（Lessons Learned）

`messages` 表的 schema 是**结构契约**，不是**语义契约**——同一个字段在不同 importer 里可能被填得完全不同；下游（dispatcher、dedupe、查询）按某种语义去用，就会出 bug。**新增 importer 或新增字段时，必须把字段语义在所有 importer 之间对齐**，并在 PR 里显式说明。

已踩过的坑：

- **`sender_display`**：backfill 从 WeFlow JSON 的 `senderDisplayName` 拿到真名；live 早期用 `senderUsername`（wxid）兜底，导致 dispatcher 按显示名查全部落空。修法：live 启动时 `/api/v1/group-members` 拉 roster 建 wxid→display 映射。**教训**：「API 没返回某字段」不是「字段缺失」，必须查清楚是不是别的 endpoint 提供，否则下游用起来会静默错。
- **`media_path`**：backfill 存项目内相对路径（`media/<group>/...`）；live 存 WeFlow 给的绝对路径（`mediaLocalPath`）。两者对同一条消息算 `dedupe_key` 的 fallback hash 不同 → 重复写入。已知边缘情况，README「冲突/幂等」段记录在案。

落实办法：
- 新 importer / 新字段定义时，**先在 `models.py` 的 docstring 里写清字段语义**，再写 importer。
- 下游模块（dispatcher 之类）若按某字段查询，**在 PR 里点名所有 importer 的填充策略**，确认对齐。
- 单元测试里至少为每个 importer 跑一条 fixture，断言关键字段语义（不是结构）相同。
