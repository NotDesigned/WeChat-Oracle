# Project notes for Claude

## 文档导航（新会话先读这段）

- **本文件**：项目特定的契约 / 用户偏好 / 踩过的坑（Lessons Learned）。冲突时**项目级 > 全局基线**。
- **`README.md`**：对外用户文档。改命令 / 配置 / schema / 入口都要回头同步。「数据流细节」段对理解三个进程怎么协作最有帮助。
- **代码内 docstring**：模块顶端有 5–10 行职责说明，`dispatcher.py` / `live.py` / `forwarded.py` 写得最详细。
- **`schema.sql`**：DDL 行级注释是字段语义的**主源**，发现描述模糊就在这儿改。
- **`.claude/hooks/check_doc_sync.py`**：自动 backstop，跨文件漂移会反向提醒。

按场景找入口：

| 我要改… | 先读 | 同步目标（见「易漂移点速查」） |
|---|---|---|
| slash 命令的语法 / 加新命令 | `dispatcher.py` 顶端 docstring + 「命令体系维护契约」 | F4 |
| 入库字段 / 新加列 | `schema.sql` + `models.py` + 「管道字段对齐」 | F1 / F2 |
| 配置项 | `config.py` `Settings` | F3 |
| CLI 子命令 | `cli.py` | F5 |
| 新 importer / 数据源 | `ingest/writer.py` 的 `write_messages` 入口 | F1 / F11 / 管道字段对齐 |
| WeFlow API 字段语义 | `live.py` 顶端 + `forwarded.py` 顶端 docstring | F11 / Lessons Learned |
| dispatcher 查询逻辑 | `dispatcher.py` 的 `fetch_candidates` | F2（要 UNION forwarded_records） |

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
- **dispatcher 冷启动不回放历史**：`_skip_backlog` 在启动时把所有未处理的 `@bot` 历史消息标 `(startup-skip)`，避免冷启动 / 大批量回灌后向群灌一通陈年答复。改这个行为要同步 README 数据流段。

## 易漂移点速查

同一个事实被多处编码就会漂移。下表列出本仓库所有冗余存储位置；改任何一处，**必须**主动看其它行的状态。`.claude/hooks/check_doc_sync.py` 覆盖 F1–F5，其它行靠人 + 本契约保障。

| ID | 事实 | 存储位置 | 自动校验 | 注意点 |
|---|---|---|---|---|
| **F1** | `messages` 表 schema | `schema.sql` + `models.py:Message` + `ingest/writer.py:INSERT_SQL/_row` + `README.md` 数据库段 | hook | 加列要四处全改 |
| **F2** | `forwarded_records` schema | `schema.sql` + `models.py:ForwardedItem` + `ingest/writer.py:INSERT_FWD_SQL` + `ingest/forwarded.py` 解析 + `README.md` | hook | 加 datatype 占位时同步 `_PLACEHOLDER` |
| **F3** | `WO_*` 配置项 | `config.py:Settings` + `README.md` 配置参考表 + `README.md` 快速上手 .env 示例 | hook | 删字段记得 .env 例子也清掉 |
| **F4** | slash 命令集 | `dispatcher.py` `Command` 子类 + `README.md` 命令详解 | hook | `Command.help()` 自动从类 attr 生成；改 attr 时同步 README |
| **F5** | CLI 子命令 | `cli.py` typer 命令 + `README.md` 快速上手 + 三进程表 | hook | 加进程要同步 README 三进程表 |
| F6 | localType→MsgType 映射 | `ingest/backfill.py:_WEFLOW_LOCAL_TYPE_MAP` + `ingest/forwarded.py` 常量 | — | live.py 通过 import 复用，单源 |
| F7 | `MsgType` enum 值 | `models.py:MsgType` + `schema.sql` `type` 列注释 + `dispatcher.py` SQL 字面量 (`'text'` 等) + `README.md` schema 段 | — | 加新 type 要同步 4 处 + dispatcher 的 `WHERE type='text'` 过滤可能要扩展 |
| F8 | 状态枚举（`Status` / `source` / `command_runs.status`） | `models.py` enum + `schema.sql` CHECK + 字面量散布 + `README.md` | SQLite CHECK 兜底 | CHECK 失败时是 IntegrityError，不会静默 |
| F9 | `dedupe_key` 拼装规则 | `models.py:compute_dedupe_key` + `schema.sql` 注释 + `README.md` 跨源去重段 | — | 改算法 = 历史数据 dedupe 失效，需要清库 |
| F10 | 显示名优先级 (groupNickname > remark > nickname > displayName) | `ingest/live.py:_pick_member_display` + `schema.sql` `sender_display` 注释 | — | backfill 走 `senderDisplayName` 单字段，没有这个优先级链 |
| F11 | WeFlow `(appmsg.type<<32)\|49` localType 编码 | `ingest/forwarded.py` 常量 + 文档 + 本文件 Lessons Learned + `README.md` 合并转发段 | — | 加新 appmsg.type（如 quote=57）若想细分要同步本表第二行 |
| F12 | 媒体子目录布局 (`images`/`voices`/`videos`/`stickers`) | `ingest/backfill.py:_MEDIA_SUBDIR` + `README.md` 媒体处理段 | — | 改字典等于把已有 `data/media/<旧>/` 全部弃用 |
| F13 | dispatcher `_skip_backlog` 启动行为 | `dispatcher.py:_skip_backlog` + `README.md` 数据流细节段 | — | 改默认行为（比如改成处理积压）必须同步 README + 给个 flag |
| F14 | 本 hook 的 marker 列表 | `.claude/hooks/check_doc_sync.py:_*_MARKERS` + 本文件「命令体系维护契约」判定标准段 | — | 改 marker 时把 prose 描述也改了，否则 hook 和契约说的不是一回事 |
| F15 | `WO_REPLY_BACKEND` 取值集 | `replier.py:build_replier` if-chain + `config.py:reply_backend` 注释 + `README.md` 配置参考表 | hook (config.py 改触发) | 加新 backend 时把这三处都同步；新建一个 `XxxReplier` 类实现 `Replier` 协议即可，不动 dispatcher |

**新事实进表的判定**：如果你引入了一个事实它**注定要在两个以上文件里出现**（即使本仓库现在只放在了一处），就把它登记到本表，并尽量用单源 + import 替代多处复制。

**两道闸**：
- `.claude/hooks/check_doc_sync.py`（PostToolUse）——Claude 编辑文件**当下**就反向提醒，看的是 working tree
- `.githooks/pre-commit`（git）——`git commit` 时**最后一道**关，看的是 staged-only 视图，挡掉手工编辑 / rebase / 不同 agent 漏掉的情况；额外还跑 schema parse / Python 语法 / API key 扫描 / `.env` 防泄露

PostToolUse 是 prompt 时机的提醒（可被忽略 / 可继续编辑修复），pre-commit 是**真硬拦**（exit 1 直接 abort commit）。两个共用同一份 `RULES` 表。

## 命令体系维护契约

`dispatcher.py` 里的 `Command` 子类（`FindCommand`、`ChatCommand`、`HelpCommand` 等）改动时，**这三处必须保持一致，否则用户拿到的帮助就是骗人的**：

1. **类属性 `name` / `usage` / `description` / `examples`** —— `Command.help()` 是 classmethod，从这些 attr 自动生成 `/help` 输出。**不用手写 help 函数体**，改 attr 就够；但反过来：改了 `parse()` 的语法（加 marker、改默认值、删 flag）就**必须**回头同步 attr，不然 `/help` 会撒谎。
2. **`README.md` 的「命令详解」段** —— 这是给人读的对外文档；CI 不会校验它和代码一致性。**任何**命令的新增 / 删除 / 重命名 / 语法改动 / 默认值改动，都必须同步这段。
3. **`parse_command` 的入口路由**（`@<bot> /xxx` 解析、`@<bot> <free text>` 兜底分支）—— 改了入口规则也算命令体系改动，README 「命令详解」+ 「错误反馈」段都要看。

**判定标准（PR / commit 自检）**：本次 diff 里 `dispatcher.py` 命中以下任一行 ≥ 1 处：
- `name = ...` / `usage = ...` / `description = ...` / `examples = [...]`（命令类常量）
- `class ...Command(Command):` / `@register`（命令本身的增删）
- `def parse(`（解析逻辑变了 → 检查 usage/examples 是否还对得上）
- `parse_command(` 函数体（路由逻辑）

→ 那 `README.md` 的「命令详解」**也必须同 PR 改**。

`.claude/hooks/check_doc_sync.py`（覆盖 F1–F5）是自动 backstop：dispatcher.py 上述行被改但 README.md 这次没动时，hook 会反过来提醒下一次工具调用。**hook 不是合规的替代**——以本契约为准，hook 失灵不是免责理由。

## 管道字段对齐（Lessons Learned）

`messages` 表的 schema 是**结构契约**，不是**语义契约**——同一个字段在不同 importer 里可能被填得完全不同；下游（dispatcher、dedupe、查询）按某种语义去用，就会出 bug。**新增 importer 或新增字段时，必须把字段语义在所有 importer 之间对齐**，并在 PR 里显式说明。

已踩过的坑：

- **`sender_display`**：backfill 从 WeFlow JSON 的 `senderDisplayName` 拿到真名；live 早期用 `senderUsername`（wxid）兜底，导致 dispatcher 按显示名查全部落空。修法：live 启动时 `/api/v1/group-members` 拉 roster 建 wxid→display 映射。**教训**：「API 没返回某字段」不是「字段缺失」，必须查清楚是不是别的 endpoint 提供，否则下游用起来会静默错。
- **`media_path`**：backfill 存项目内相对路径（`media/<group>/...`）；live 存 WeFlow 给的绝对路径（`mediaLocalPath`）。两者对同一条消息算 `dedupe_key` 的 fallback hash 不同 → 重复写入。已知边缘情况，README「冲突/幂等」段记录在案。
- **`localType` 高位编码**：WeFlow 把 `<appmsg>.<type>` 偷塞进 localType 高 32 位（`localType = (appmsg.type << 32) | 49`）。早期 `_WEFLOW_LOCAL_TYPE_MAP` 只查整个 localType，对所有 app 消息都返回 None → **整条消息被 importer 丢弃**（包括合并转发、引用、链接卡片）。修法：所有查表前先 `base_local_type(lt)` 抹掉高位；需要细分的（如 `appmsg.type=19` 合并转发）用 `appmsg_subtype(lt)` 取出来另判。**教训**：第三方 API 返回的「整数字段」可能是组合编码，光按枚举查表会静默吃掉一类消息——加新格式前，先把分布拉出来看一眼。
- **`forwarded_records.t` 不是入库时间**：合并转发子项的 `t` 来自源群里原消息的 `<srcMsgCreateTime>`，可能比包它进来的 wrapper 早数年。dispatcher 的 `since:` 过滤要按子项自己的 `t` 算（不是 wrapper 的 `t`），否则 `since:2024` 这种查询会漏掉「2024 的消息被 2026 转发进来」的情况。

落实办法：
- 新 importer / 新字段定义时，**先在 `models.py` 的 docstring 里写清字段语义**，再写 importer。
- 下游模块（dispatcher 之类）若按某字段查询，**在 PR 里点名所有 importer 的填充策略**，确认对齐。
- 单元测试里至少为每个 importer 跑一条 fixture，断言关键字段语义（不是结构）相同。
