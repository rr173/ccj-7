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
  会话状态为三态：
  - `assembling` —— 目前连续，但还没见到 `is_last`，仍在拼接；
  - `incomplete` —— 中间有缺口，`content` 里嵌显式标记 `[缺口:片段3]`；
  - `complete` —— 已见 `is_last` 且 1..last_seq 全部到齐。
- **缺口补上后可见"曾经缺过"**：缺口出现和补齐都记在 `gap_history`
  （`detected_at` / `filled_at`），记录永不删除；`was_incomplete` 恒为
  `true`；`version` 单调递增，客户端能发现已发布的视图发生了变化。
- **重启不散**：全部状态在 SQLite（WAL + `synchronous=FULL`），视图由
  片段表现算，进程重启、容器重建后会话原样恢复，可继续拼接。

## API

```
POST /fragments            接收片段，返回 {ingest, session} 当前视图
GET  /sessions             所有会话摘要
GET  /sessions/{call_id}   单个会话完整视图（不存在返回 404）
GET  /healthz              健康检查
GET  /docs                 Swagger UI
```

会话视图关键字段：`status`、`content`（拼好的文本，一行一片段）、
`gaps`（当前缺口区间）、`was_incomplete`、`gap_history`、`version`、
`retransmissions`、`conflicts`、`completed_at`。

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
tests/           16 个测试：乱序、重传、通话隔离、缺口、重启持久化
Dockerfile / docker-compose.yml
```

## 已知边界

- `call_id` 必须由上游保证唯一标识一次通话；若链路会复用 call_id，
  需要在上游拼接链路口令后再送入。
- 越过已知 `is_last` 序号的片段、矛盾的结束标记会照收并计入
  `conflicts`，由人工介入处理。
- 单实例服务；多副本部署需要把存储换成共享数据库（表结构可直接平移
  到 PostgreSQL）。
