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

## API

```
POST /fragments                        接收片段，返回 {ingest, session} 当前视图
GET  /sessions                         所有会话摘要
GET  /sessions/{call_id}               单个会话完整视图（不存在返回 404）—— 永远是最新样子
POST /sessions/{call_id}/drafts        把当前视图钉成一稿对外给出，返回稿号（201）
GET  /sessions/{call_id}/drafts        该通话已发出的全部稿（按发稿顺序）
GET  /sessions/{call_id}/drafts/{n}    取该通话的第 n 稿
GET  /drafts/{draft_no}                按稿号取已发稿 —— 永远是当时那一稿
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
`issued_at`。稿一旦发出即冻结，任何后续片段都不会改变它。

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
tests/           34 个测试：乱序、重传、通话隔离、缺口（含越界/收缩/无结束标记
                 补齐/大空洞）、旧表迁移、重启持久化，以及已发稿的钉住、
                 订正链、两通电话隔离、重启后稿不丢
Dockerfile / docker-compose.yml
```

## 已知边界

- `call_id` 必须由上游保证唯一标识一次通话；若链路会复用 call_id，
  需要在上游拼接链路口令后再送入。
- 越过已知 `is_last` 序号的片段、矛盾的结束标记会照收并计入
  `conflicts`，由人工介入处理。
- 单实例服务；多副本部署需要把存储换成共享数据库（表结构可直接平移
  到 PostgreSQL）。
