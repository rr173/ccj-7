# 通话片段拼接服务

把同一条链路上**乱序到达、带重传**的通话片段拼回完整会话的 HTTP 服务。

## 片段格式约定

上游链路投递片段时携带以下字段（`POST /fragments`）：

| 字段 | 含义 |
|---|---|
| `call_id` | 唯一标识一次通话。**不同通话必须用不同 call_id** |
| `seq` | 从 1 开始的序号，同一次通话内按序分配 |
| `text` | 片段内容（一句/一段） |
| `is_last` | 该通话最后一个片段置 `true`，其 `seq` 即片段总数 |

## 需求对应的机制

- **同一次通话拼在一起、两次通话不互织**：所有状态严格按 `call_id` 归组，
  不同 `call_id` 的片段物理上落在不同的行里，不可能交叉。
- **重传不产生重复句子**：`(call_id, seq)` 是主键。内容相同的重传幂等去重
  （只累计 `retransmissions`）；同号不同文视为冲突，保留先到者并计入
  `conflicts`。
- **缺段不假装完整**：已收到最大序号为 M 时，1..M 中未到的号就是**缺口**。
  判定只看“实际到过的序号”，与 `is_last` 声明在第几号无关——哪怕结束标记
  早就见过，后来又有越过结束序号的片段到达、正文里仍夹着缺口，也绝不报
  `complete`（越界片段本身照常计入 `conflicts`）。会话状态为三态：
  - `assembling` —— 目前连续，但还没见到 `is_last`（或结尾片段还在路上）；
  - `incomplete` —— 中间有缺口，`content` 里嵌显式标记 `[缺口:片段3]`；
  - `complete` —— 已见 `is_last` 且到过的序号连成一片、越过了结束序号；
    **或曾经缺过（对外给过 incomplete），缺口现已全部补齐**——即便结束标记
    一直没到也算完整，不会停在拼接中。
- **缺口补上后可见"曾经缺过"**：缺口出现和补齐都记在 `gap_history`
  （按**区间**记录 `range` / `detected_at` / `filled_at`），记录永不删除；
  缺口缩小时残留段继承原发现时间，整段补齐后历史行关闭但保留；
  `was_incomplete` 恒为 `true`；`version` 单调递增，客户端能发现已发布的
  视图发生了变化。
- **序号空一大截也不卡**：缺口全程以闭区间表示和对账（`gap_events` 同样按
  区间存储），绝不按序号逐个展开。只收到 1 和 100 亿时，缺口就是一行
  `[2, 9999999999]`，写入、查询、列表都在毫秒级，且立刻看得出缺着。
- **重启不散**：全部状态在 SQLite（WAL + `synchronous=FULL`），视图由
  片段表现算，进程重启、容器重建后会话原样恢复，可继续拼接。
- **对外给出的稿钉住不动**：`GET /sessions/{call_id}` 永远是最新样子，
  所以"对外给出"是一个显式动作——`POST /sessions/{call_id}/drafts`
  把**那一刻**的正文、缺口、状态整体快照成一稿，发出稿号
  （如 `C1-D0001`）。稿表只插不改：之后补段、重传、再发新稿都不动
  这一稿，拿稿号查到的永远是当时那份正文和缺口。缺口补上后再发一稿，
  新稿自带 `supersedes`（订正的是哪一稿）和 `predecessor_gaps`
  （上一稿当时缺在哪），"曾经缺过"直接可查。稿号带着 `call_id`，
  两通电话的稿号空间天然不相交，稿不可能串；已发的稿同样落在
  SQLite 里，重启后还在。会话视图里的 `latest_draft.changed_since`
  提示活视图相对最近一稿是否已有变化（该出新稿的信号）。
- **发出去的稿要交给下游签收**：稿一发出就自动进入待签——签收单与稿在
  同一事务里出生，一稿一张，随稿落 SQLite。按稿号签收；签收单上带稿号、
  通话、第几稿和签收时间，签完看得出签的是哪一稿。待签期间取出，看到的
  仍是发稿那一刻的正文和缺口——签收单嵌的就是 drafts 表里那份
  INSERT-only 的快照，后来补段、重传、会话变新都碰不到它。签收单与稿
  一对一：没签时又出了新稿，旧待签仍是旧那份，不会被新稿顶掉；同一稿
  重复签收返回 409，首次签收时间不动。稿号带着 `call_id`，拿一通的号
  签不到另一通的稿，两通电话的待签和签收不串。重启后没签完的原样还在；
  老库升级时已有的稿会自动补上待签记录。
- **已发出的稿按顺序听回去**：回放按**稿号**开始（`POST /drafts/{稿号}
  /playback`），一稿一条独立进度，从稿的第 1 个拼装单元顺序往后。轮到
  **发稿当时的缺口**必须停住：状态变 `blocked`、位置不动、`heard` 为空，
  再推也不前进——不能跳过缺口当成听完。回放单元列表取自 drafts 表那份
  INSERT-only 快照，所以**后来补上的段不会让这条回放突然变完整**：旧稿
  仍停在当时的缺口前，要听补齐后的内容得另发新稿、另开一条回放，两条
  回放互不干扰（旧稿继续 blocked，新稿可一路 finished）。进度落 SQLite，
  听到哪重启后还停在那儿；重复“开始”只回到当前进度，绝不重头再听。
  稿号自带 `call_id`，按通话列出（`GET /sessions/{call_id}/playbacks`）
  也带 `call_id` 过滤，两通电话的回放物理上不串。回放代码路径只写新的
  `playbacks` 表、只读 `drafts`：**不把稿改掉，也不把待签签掉**。
- **未签收的已发稿可以按稿号撤回**：`POST /drafts/{稿号}/withdrawal` 按
  稿号撤回。撤回只新增一条 `withdrawals` 记录，不 UPDATE `drafts`：那一稿
  当时的正文、拼装单元、缺口和发稿时间仍原样可查；撤回记录和稿上的
  `is_withdrawn=true`、`withdrawn_at` 都带稿号、通话和第几稿，撤完看得出
  撤的是哪一份。签收与撤回互斥：**已经签过的不能撤，撤回后的未签稿不能再
  签**。正在听的稿撤回后，`playbacks.position` 停在听到的位置，状态变
  `withdrawn`，之后不能继续推进，也不能重新开始；没开始过的稿撤回后同样
  不能再开。稿号自带 `call_id`，全局稿号操作也撤不到另一通电话；撤回记录
  落 SQLite，重启后仍是终态。
- **稿发出后才到的段单独记迟到**：某通电话发出至少一稿后，新入库的片段
  （内容相同的重传不算——它没带来新东西）会单独记进迟到记录，挂上到达
  时最新的一稿，看得出补在哪一稿后面；接收响应里 `ingest.late=true`
  当场标出。迟到记录只往自己的表里插行：已发的稿一字不改、待签原样
  欠着、停在缺口上的回放不会突然听完。记录按 `call_id` 归组，按通话
  查（`GET /sessions/{call_id}/late-fragments`）或按稿查
  （`GET /drafts/{draft_no}/late-fragments`）都带着标识，两通电话的
  迟到记录不串；同样落 SQLite，重启后还在。

## API

```
POST /fragments                        接收片段，返回 {ingest, session} 当前视图
GET  /sessions                         所有会话摘要
GET  /sessions/{call_id}               单个会话完整视图（不存在返回 404）—— 永远是最新样子
POST /sessions/{call_id}/drafts        把当前视图钉成一稿对外给出，返回稿号（201）
GET  /sessions/{call_id}/drafts        该通话已发出的全部稿（按发稿顺序）
GET  /sessions/{call_id}/drafts/{n}    取该通话的第 n 稿
GET  /drafts/{draft_no}                按稿号取已发稿 —— 永远是当时那一稿
POST /drafts/{draft_no}/receipt        按稿号签收（201；已签过 409；未知稿号 404）
GET  /drafts/{draft_no}/receipt        该稿的签收单（待签/已签 + 当时那稿的正文和缺口）
GET  /sessions/{call_id}/receipts      该通话的全部签收单（按发稿顺序）
POST /drafts/{draft_no}/withdrawal     按稿号撤回未签稿（/withdraw 为别名；201；已签/已撤 409；未知稿号 404）
GET  /drafts/{draft_no}/withdrawal     这一稿的撤回记录（未撤回 404）
GET  /sessions/{call_id}/withdrawals   该通话已撤回的全部稿（按发稿顺序）
POST /drafts/{draft_no}/playback       拿稿号开始回放（首次 201；已开始 200 且只回当前进度）
GET  /drafts/{draft_no}/playback       听到哪了（未开始 409；未知稿号 404）
POST /drafts/{draft_no}/playback/advance  按顺序听下一段（blocked 时位置不动）
GET  /sessions/{call_id}/playbacks     该通话已开始的全部回放（按发稿顺序）
GET  /sessions/{call_id}/late-fragments  该通话的全部迟到片段（按到达顺序，各自补在哪一稿后面）
GET  /drafts/{draft_no}/late-fragments   补在这一稿后面的迟到片段
GET  /healthz                          健康检查
GET  /docs                             Swagger UI
```

会话视图关键字段：`status`、`content`（拼好的文本，一行一片段）、
`gaps`（当前缺口区间列表，如 `[[2,4]]`）、`was_incomplete`、`gap_history`
（每项含 `range`/`detected_at`/`filled_at`）、`version`、
`retransmissions`、`conflicts`、`completed_at`、`latest_draft`
（最近一稿的 `draft_no`/`issued_at`/`changed_since`，未发过稿为 `null`）。

稿（draft）关键字段：`draft_no`（稿号）、`draft_seq`（该通话第几稿）、
`status`/`content`/`gaps`（发稿那一刻的状态、正文、缺口）、`gap_history`
（截至当时的缺口历史）、`supersedes`（本稿订正的上一稿稿号）、
`predecessor_had_gaps`/`predecessor_gaps`（上一稿当时是否带缺口、缺在哪）、
`issued_at`、`is_withdrawn`/`withdrawn_at`（是否撤回、撤回时刻）。稿一旦
发出即冻结，撤回只叠加状态，任何后续片段或撤回动作都不会改变其正文和缺口。

签收单（receipt）关键字段：`draft_no`/`call_id`/`draft_seq`（签的是哪一稿）、
`status`（`pending` 待签 / `signed` 已签 / `withdrawn` 已撤回）、
`issued_at`（发稿时间）、`signed_at`（签收时间，未签为 `null`）、
`withdrawn_at`（撤回时间，未撤为 `null`）、`draft`（当时那一稿的完整快照——
正文、缺口原样嵌在签收单里，不随后续片段变化）。

回放（playback）关键字段：`draft_no`/`call_id`/`draft_seq`（听的是哪稿）、
`status`（`playing` 下一段是正常片段 / `blocked` 轮到发稿当时的缺口、停住 /
`finished` 这稿快照已按序听完 / `withdrawn` 稿已撤回、听到哪停在哪 /
`not_started` 还没拿稿号开始）、`position`
（已听到第几个拼装单元，下次从这里继续）、`total_units`、`next`（下一个单元：
正常片段为 `{seq,text}`，缺口为 `{gap:[lo,hi],marker}`，末尾为 `null`）、
`heard`（仅推进响应：本次刚听到的片段；停在缺口或已听完为 `null`）、
`started_at`/`updated_at`/`finished_at`、`draft`（回放所基于的当时那稿快照）。
回放只新增 `playbacks` 进度，从不修改 `drafts`，也不触碰 `receipts`。

撤回记录（withdrawal）关键字段：`draft_no`/`call_id`/`draft_seq`（撤的是哪稿）、
`withdrawn_at`（撤回时刻）、`receipt_status`（撤回时的终态，通常为 `withdrawn`）、
`draft`（发稿当时完整快照）。撤回不删除稿或签收单，只让该稿进入不可签、
不可继续听的终态。

迟到片段（late fragment）关键字段：`call_id`、`seq`、`text`（到达时的原文）、
`after_draft_no`/`after_draft_seq`（补在哪一稿后面——到达那一刻该通话最新的
一稿）、`arrived_at`。接收片段的响应里 `ingest.late` 为 `true` 表示这一段是
发稿后才到的。迟到记录只往 `late_fragments` 表插行，从不修改 `drafts`、
`receipts`、`playbacks`。

### 示例

```bash
# 乱序 + 缺第 3 段
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":1,"text":"你好"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":2,"text":"听得到吗"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":4,"text":"那就这样","is_last":true}'

curl localhost:8000/sessions/C1
# status=incomplete, content 中含 "[缺口:片段3]"

# 重传第 2 段（幂等，不产生重复句子）
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":2,"text":"听得到吗"}'

# 补上缺口 → 同一条会话变 complete，was_incomplete=true
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":3,"text":"信号不太好"}'
```

### 对外给出与订正

```bash
# 缺第 3 段时就得先对外给一版 → 钉成 C1-D0001（正文含 [缺口:片段3]）
curl -X POST localhost:8000/sessions/C1/drafts
# {"draft_no":"C1-D0001","status":"incomplete","gaps":[[3,3]],...}

# 缺口补上后出新稿 → C1-D0002，看得出订正的是哪一稿、上一稿曾缺在哪
curl -X POST localhost:8000/sessions/C1/drafts
# {"draft_no":"C1-D0002","status":"complete","supersedes":"C1-D0001",
#  "predecessor_had_gaps":true,"predecessor_gaps":[[3,3]],...}

# 之后任何时候拿旧稿号，拿到的仍是当时那一稿的正文和缺口
curl localhost:8000/drafts/C1-D0001
# {"status":"incomplete","gaps":[[3,3]],"content":"你好\n听得到吗\n[缺口:片段3]\n那就这样",...}
```

### 下游签收

```bash
# 发出去的稿自动进入待签；下游按稿号签收
curl -X POST localhost:8000/drafts/C1-D0001/receipt
# {"draft_no":"C1-D0001","call_id":"C1","draft_seq":1,"status":"signed",
#  "signed_at":"...", "draft":{...当时那一稿...}}
# 同一稿再签一次 → 409

# 待签时取出，仍是发稿那一刻的正文和缺口；已签的看得出签的是哪一稿
curl localhost:8000/drafts/C1-D0001/receipt

# 该通话的全部签收单：哪些还欠着、哪些已签
curl localhost:8000/sessions/C1/receipts
```

### 撤回未签收稿

```bash
# C1-D0001 未签，拿稿号撤回；响应里仍带完整快照，看得出撤的是哪一稿
curl -X POST localhost:8000/drafts/C1-D0001/withdrawal
# {"draft_no":"C1-D0001","call_id":"C1","draft_seq":1,
#  "withdrawn_at":"...","receipt_status":"withdrawn","draft":{...当时那稿...}}

# 再查稿会带撤回标记，但正文和缺口仍是发稿时的样子
curl localhost:8000/drafts/C1-D0001
# {"is_withdrawn":true,"withdrawn_at":"...","content":"...原样...","gaps":[...]}

# 撤过不能再签；已签过的稿反过来不能撤
curl -X POST localhost:8000/drafts/C1-D0001/receipt        # 409
curl localhost:8000/drafts/C1-D0001/withdrawal            # 撤回记录
curl localhost:8000/sessions/C1/withdrawals               # 按通话列出撤回

# 正在听到一半撤回：回放 position 停住，之后推进/重开都是 withdrawn
curl -X POST localhost:8000/drafts/C1-D0001/playback/advance
# {"status":"withdrawn","position":1,"next":null,...}
```

### 按顺序听回去

```bash
# 缺第 3 段时已对外给过 C1-D0001（正文含 [缺口:片段3]）
# 拿稿号开始听，从第 1 段顺序往后
curl -X POST localhost:8000/drafts/C1-D0001/playback
# {"status":"playing","position":0,"total_units":4,"next":{"seq":1,"text":"你好"},...}

curl -X POST localhost:8000/drafts/C1-D0001/playback/advance
# {"status":"playing","position":1,"heard":{"seq":1,"text":"你好"},
#  "next":{"seq":2,"text":"听得到吗"},...}
curl -X POST localhost:8000/drafts/C1-D0001/playback/advance
# {"status":"blocked","position":2,"heard":null,
#  "next":{"gap":[3,3],"marker":"[缺口:片段3]"},...}   # 轮到当时的缺口，停住

# 再推也不动：不能跳过缺口当成听完
curl -X POST localhost:8000/drafts/C1-D0001/playback/advance
# 仍 {"status":"blocked","position":2,...}

# 缺口后来补上、会话已 complete —— 但这条旧回放不会突然变完整，仍停在缺口前；
# 补齐后另发了新稿 C1-D0002，想听完整内容得拿新稿号另开一条回放
curl -X POST localhost:8000/drafts/C1-D0002/playback
# 两条回放互不干扰：旧稿继续 blocked，新稿从 0 开始可一路 finished

# 听到哪了（重启后仍是这个位置）；重复“开始”只回到当前进度，绝不重头
curl localhost:8000/drafts/C1-D0001/playback
# {"status":"blocked","position":2,...}

# 该通话已开始的全部回放（按发稿顺序），列不出另一通电话的回放
curl localhost:8000/sessions/C1/playbacks
```

回放只读稿快照、只写自己的进度：`GET /drafts/C1-D0001` 拿到的稿一字不变，
`GET /drafts/C1-D0001/receipt` 仍是 `pending`——听稿不改稿、不签收。

### 稿发出后才到的段

```bash
# C1-D0001 带着缺口 [缺口:片段3] 发出后，第 3 段才姗姗来迟
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":3,"text":"信号不太好"}'
# {"ingest":{"stored":true,"duplicate":false,"conflict":false,"late":true},...}

# 已发的稿、待签、停在缺口上的回放全都原样不动；迟到的段单独可查，
# 看得出补在哪一稿后面
curl localhost:8000/sessions/C1/late-fragments
# {"late_fragments":[{"call_id":"C1","seq":3,"text":"信号不太好",
#   "after_draft_no":"C1-D0001","after_draft_seq":1,"arrived_at":"..."}]}
curl localhost:8000/drafts/C1-D0001/late-fragments   # 补在这一稿后面的段

# 内容相同的重传不算迟到（没带来新东西）；想对外给补齐后的内容，照
# 例另发新稿（C1-D0002），之后新到的段就记在 D0002 后面
```

## 运行

### Docker

```bash
docker build -t call-reassembly .
docker run -d -p 8000:8000 -v reassembly-data:/data call-reassembly
# 或
docker compose up -d
```

SQLite 落在容器 `/data/sessions.db`（环境变量 `DB_PATH` 可改），
挂卷后重启/重建容器不丢拼接中的会话。

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
python -m pytest tests/ -q
```

## 项目结构

```
app/
  main.py        HTTP API 层（FastAPI）
  store.py       SQLite 持久化：写入去重、缺口对账、视图拼装
  reassembly.py  纯函数：重排、缺口检测、状态判定（便于单测）
  models.py      片段入参校验
tests/           69 个测试：乱序、重传、通话隔离、缺口（含越界/收缩/无结束标记
                 补齐/大空洞）、旧表迁移、重启持久化，已发稿的钉住、订正链、
                 两通电话隔离、重启后稿不丢，签收（待签钉住、按号签收、
                 不串签、不重复签、新稿不顶旧待签、重启后待签还在、老库补单），
                 回放（顺序收听、缺口停住不跳过、补段不让旧稿变完整、
                 新旧稿两条回放独立、两通电话不串、重启停在原处、不改稿不签收），
                 撤回（按号撤回、快照不改正文缺口、已签不可撤、撤后不可签、
                 正在听停在原处、两通电话不串、重启后仍是撤回态），
                 以及迟到片段（单独记录、看得出补在哪一稿后面、不改稿不动待签
                 不让回放突然听完、两通电话不串、重启后记录还在）
Dockerfile / docker-compose.yml
```

## 已知边界

- `call_id` 必须由上游保证唯一标识一次通话；若链路会复用 call_id，
  需要在上游拼接链路口令后再送入。
- 越过已知 `is_last` 序号的片段、矛盾的结束标记会照收并计入
  `conflicts`，由人工介入处理。
- 单实例服务；多副本部署需要把存储换成共享数据库（表结构可直接平移
  到 PostgreSQL）。
