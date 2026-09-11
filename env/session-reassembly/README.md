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
- **发出去的稿要先有人认领**：`POST /drafts/{draft_no}/claim` 按稿号认领，
  没认领的稿不能签收、不能回放、不能撤回，**也不能往下游投**（一律 409）。一稿同一时刻
  至多一个人持有：一个人认了，别人再认是 409，认不走；本人重复认领幂等
  （200，时间不动）。当前认领人 `POST .../claim/release` 把稿交出去之后，
  别人才能认领；交出后、还没人认期间同样不能签/听/撤/投。认领只往 `claims`
  表写记录，绝不碰 `drafts`——那一稿当时的正文和缺口一个字不变；每次
  认领/交出都在 `history` 里留痕。稿号自带 `call_id`，拿一通的号认不到
  另一通的稿；认领记录落 SQLite，重启后认了谁还在。
- **发出去的稿要能往下游投**：`POST /drafts/{draft_no}/delivery` 按稿号投，
  投一次在 `deliveries` 表落一行（第几次投、何时投），投完看得出投的是哪一稿
  （记录带稿号、通话、第几稿，并嵌发稿当时的稿快照）。**没人认领的不能投**；
  **投出去还没回音之前不能再投一次**（409）。下游可以
  `POST .../delivery/accept` **收下**，也可以 `POST .../delivery/return`
  **退回**：**退了之后才能再投**（新起一行、attempt 递增，历次投递都留痕）；
  **收下之后不能再退也不能再投**（终态，重复答复 409，首次时间不动）。投递、
  退回、再投、收下只写 `deliveries` 表，**绝不改那一稿当时的正文和缺口**
  （`drafts` 仍是 INSERT-only）。稿号自带 `call_id`，两通电话的投递不串，
  拿一通的号投不走另一通的稿；投递记录落 SQLite，**重启后投到哪了还在**。
  稿撤过不能投；投出去待回音、或已被下游收下的稿也不能撤回（退回后不拦）。
- **稿发出后才到的段单独记迟到**：某通电话发出至少一稿后，新入库的片段
  （内容相同的重传不算——它没带来新东西）会单独记进迟到记录，挂上到达
  时最新的一稿，看得出补在哪一稿后面；接收响应里 `ingest.late=true`
  当场标出。迟到记录只往自己的表里插行：已发的稿一字不改、待签原样
  欠着、停在缺口上的回放不会突然听完。记录按 `call_id` 归组，按通话
  查（`GET /sessions/{call_id}/late-fragments`）或按稿查
  （`GET /drafts/{draft_no}/late-fragments`）都带着标识，两通电话的
  迟到记录不串；同样落 SQLite，重启后还在。
- **发出去的稿要能出勘误**：`POST /drafts/{draft_no}/errata` 按稿号对
  某一稿的某一段出勘误，记录带稿号、通话、第几稿、对着哪一段、当时的
  原文和改成什么，看得出勘的是哪一稿的哪一段。勘误只往 `errata` 表
  插行，**绝不改那一稿当时的正文和缺口**（`drafts` 仍是 INSERT-only）。
  **没人认领的不能出**；**同一段不能出两次**（409，原记录时间不动）；
  对着的段必须是该稿快照里真实存在的片段（缺口不是段、越界序号也不是）；
  撤过的稿不能出，已签收、已投下游的稿照样能出。稿号自带 `call_id`，
  拿一通的号给另一通出不了勘误，按通话列出
  （`GET /sessions/{call_id}/errata`）也带着过滤，两通电话的勘误不串；
  勘误记录落 SQLite，重启后出过的勘误还在。
- **有的稿得压着、写明几点几分才能见**：`POST /drafts/{draft_no}/hold`
  按稿号把一稿压到 `release_at` 那个钟点。没到点之前，任何取稿的地方——
  按稿号、按通话列表、签收单、认领、回放、投递、勘误——都只能知道这稿
  **还压着、何时解**（`is_held=true`、`visibility=held`、`release_at`），
  正文、拼装单元、缺口、缺口历史、状态等快照字段**一律为 null**；勘误
  历史里的原文/新文本也抹掉，回放不能开始或推进、下游不能投、勘误不能出。
  **到点不用再做任何操作**：解禁状态只由“当前钟点 ≥ release_at”实时算出，
  再拿同一稿号，看到的就是发稿当时钉住的正文和缺口（没有“解开”这个写
  动作，也就不可能提前解开）。约束：**解禁时刻不能早于这稿发出的时刻**
  （422，稿不动）；**写上去就不能改、一稿只能压一次**（重复压 409，已到点
  自动解禁后同样不能重压）；**同一通电话里，后发出的稿不能比先发出的稿
  更早解**（409；按 `draft_seq` 与该通话已压各稿对账，同一时刻允许）。
  压稿只往 `holds` 表插一行，绝不碰 `drafts`——压不压、解没解都不改那
  一稿当时的正文和缺口；认领、签收、撤回等元数据动作压着期间照常。
  `GET /drafts/{draft_no}/hold` 看这一稿压到何时、现在还压着没有（没压过
  404），`GET /sessions/{call_id}/holds` 按通话列出。稿号自带 `call_id`，
  两通电话的压稿不串；**重启后仍按钟点判：没到点的继续看不见，到了点的
  不用再压一次**。

- **两通不同的电话可以搭成一座桥**：`POST /bridges` 拿两通**不同的、非空**
  的通话搭一座桥（桥号 `B0001` 全局递增，与稿号空间不相交）。桥上按序号
  `1..N`（N=两边实际到过的最大序号）**一对一对齐**：同一个序号两边都到了
  才算一对 `aligned`；**一边缺了，这一对就是缺口**（标明缺左还是缺右，到了
  的那边的字原样带着、缺的那边明确为 `null`）——**绝不拿另一边的字凑上**。
  两边都没到的连续序号合成一个区间单元，序号空一大截也不逐号展开。对齐
  进度随时可查：`aligned_count`/`gap_count`/`total_pairs`、缺口区间 `gaps`，
  以及连续对齐前缀 `aligned_up_to`（第一对就没对上为 0）。**活动桥的对齐
  永远从两边片段现算**：后来缺的那边补上了，再看同一座桥就多对上一对。
  桥只新增 `bridges` 表，**不写不改两边的片段**：两通电话始终是各自独立的
  会话，各自继续收段、发稿互不影响。
- **同一通电话不能同时待在两座桥里**：搭桥时两边都查活动桥（含左右交叉的
  情形），数据库还有两条部分唯一索引 + 触发器兜底（`IntegrityError`）。
  **拆桥**（`POST /bridges/{桥号}/dismantle`）后两通恢复自由，可再搭新桥；
  空通话、同一通跟自己搭都拒绝（409/404）。
- **拆掉以后两通还是各自的会话、对齐到哪一对要留得下来**：拆桥只把桥状态
  置 `dismantled`，并把**那一刻**两边通话与逐对对齐整体快照进桥行
  （`snapshot_json`），不删任何片段。之后两通继续收段，这座已拆的桥再看
  永远是拆时那份对齐（`aligned_up_to`、缺口、各对两边的字都钉住）；重复拆
  409，时间不动。按桥号（`GET /bridges/{桥号}`）、列全部桥
  （`GET /bridges`）或按通话列（`GET /sessions/{call_id}/bridges`，标明
  该通在左还是右）都查得到，两通电话的桥列不串。
- **服务再起来，没拆的桥还在、对齐还对得上**：桥行、拆桥快照都落 SQLite
  （WAL + `synchronous=FULL`）。重启后活动桥仍是 `active`，按到过的序号
  重新现算对齐；已拆桥仍是拆时的冻结快照；活动桥唯一约束重启后继续生效。
- **桥还搭着时可以把此刻的对齐拍下来**：`POST /bridges/{桥号}/photos` 给一座
  **还搭着**的桥拍一张照，把**那一刻**两边身份与逐对对齐（`pairs`、
  `aligned_up_to`、`gaps`、各对两边的字）整体快照进 `bridge_photos` 表（只插
  不改），发照片序号 `photo_seq`——**同一座桥可以拍好几次**，每张看得出是第几张、
  何时拍、当时对到哪一对。拍完之后两通再来新段、缺口补齐，只让活动桥的**现行**
  对齐继续现算，旧照片一个字不变（`GET /bridges/{桥号}/photos` 列全部，
  `GET /bridges/{桥号}/photos/{n}` 取第 n 张）。拍照不写、不改、不删两通各自的
  片段/会话，也不碰 `bridges` 行——桥上此刻还在算的对齐照旧按两边现在的段现算；
  桥视图里带 `photo_count`/`photos`，每张照片都嵌着自己拍时那份冻结对齐。
  **已经拆掉的桥不能再拍**（409；拆时对齐另有拆桥快照留痕），但拆前拍过的照片
  拆后照样可查、仍是拍时那份；换边后旧照片仍钉着拍照当时的旧两边和旧对齐，不被
  新两边盖掉。照片落 SQLite：**服务再起来，拍过的还在，现行对齐仍按两边现在的
  段来算**。
- **桥还搭着可以换掉其中一边**：`POST /bridges/{桥号}/swap` 指明换哪一边  （`side=left|right`）和换上来的通话。**换完还是这座桥**——桥号不变、状态
  仍 `active`，现行对齐立刻按**新的两边**重新现算；被换下去的那通**恢复
  自由**（会话视图 `active_bridge` 清空，可再搭新桥、日后也能再被换回来），
  它的片段一个不删不改。换上来的那通必须是**已经有片段**的非空通话，且
  **不能已经待在别的活动桥里**（同一通不能同时待在两座桥里，触发器+唯一
  索引在换边的 UPDATE 上同样兜底），也不能就是这座桥当前两边中的任何一通；
  **已经拆掉的桥不能再换边**（409）。换边只往 `bridge_swaps` 表 INSERT 一行、
  再改桥行那一列，**绝不写不改任何片段**。**换边当时旧的两边是谁、对到哪一
  对要能事后查到、不被新两边盖掉**：每次换边的留痕（第几次、换哪边、换下/
  换上/不动各是谁、何时换）都嵌着换边**前**的完整对齐快照 `before`（旧两边
  身份、逐对单元、`aligned_up_to`、缺口），只插不改——再换边、拆桥都另写
  别处，这一行一个字不动；`GET /bridges/{桥号}` 带 `swap_count`/`swaps`，
  `GET /bridges/{桥号}/swaps` 专查历次留痕。换边落 SQLite：**服务再起来，
  换过边的桥还在，现行对齐按新两边对得上，旧两边的旧对齐仍查得到**；被换
  下去的那通重启后照样自由。按通话列桥
  （`GET /sessions/{call_id}/bridges`）把换下去/换上来的经过也列得出：
  被换下去的条目标 `currently_on_bridge=false`，并给出它最后在桥时的左右侧。

## API

```
POST /fragments                        接收片段，返回 {ingest, session} 当前视图
GET  /sessions                         所有会话摘要
GET  /sessions/{call_id}               单个会话完整视图（不存在返回 404）—— 永远是最新样子
POST /sessions/{call_id}/drafts        把当前视图钉成一稿对外给出，返回稿号（201）
GET  /sessions/{call_id}/drafts        该通话已发出的全部稿（按发稿顺序）
GET  /sessions/{call_id}/drafts/{n}    取该通话的第 n 稿
GET  /drafts/{draft_no}                按稿号取已发稿 —— 永远是当时那一稿；压着时只给“还压着、何时解”
POST /drafts/{draft_no}/hold            把稿压到指定时刻才见（201；压过 409；早于发稿 422；解禁逆序 409）
GET  /drafts/{draft_no}/hold            这一稿压到何时、现在还压着没有（没压过 404）
GET  /sessions/{call_id}/holds          该通话压过的全部稿（按发稿顺序）
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
POST /drafts/{draft_no}/claim            按稿号认领（201；本人重复认领 200；别人已认领 409；未知稿号 404）
POST /drafts/{draft_no}/claim/release    把当前认领交出去，之后才能换人认（200；未认领 409）
GET  /drafts/{draft_no}/claim            这一稿现在谁认着（含历次认领/交出记录）
GET  /sessions/{call_id}/claims          该通话全部稿的认领状态（按发稿顺序）
POST /drafts/{draft_no}/delivery         按稿号往下游投（201；待回音/已收下/已撤回/未认领 409；未知稿号 404）
POST /drafts/{draft_no}/delivery/accept  下游收下（200；终态，不能再退/再投；没在等回音 409）
POST /drafts/{draft_no}/delivery/return  下游退回（200；退了之后才能再投；没在等回音 409）
GET  /drafts/{draft_no}/delivery         这一稿投到哪了（当前状态 + 历次投递，未投过 status=none）
GET  /sessions/{call_id}/deliveries      该通话全部稿的投递状态（按发稿顺序）
POST /drafts/{draft_no}/errata           按稿号对某一段出勘误（201；未认领/已撤回/段不在稿里/该段已出过 409；未知稿号 404）
GET  /drafts/{draft_no}/errata           这一稿出过的全部勘误（按段序）
GET  /sessions/{call_id}/errata          该通话出过的全部勘误（按发稿顺序、段序）
POST /bridges                            拿两通不同的电话搭一座桥（201；空通话/同一通/已在活动桥 409；未知通话 404）
GET  /bridges                            全部桥（搭着的和已拆的，按搭桥顺序）
GET  /bridges/{bridge_no}                按桥号取桥：两边身份、逐对对齐、缺口缺哪一边（活动桥现算/已拆桥给拆时快照）
POST /bridges/{bridge_no}/dismantle      拆桥（两通恢复各自独立、对齐进度冻结留痕；已拆 409；未知桥号 404）
POST /bridges/{bridge_no}/swap           换掉活动桥的其中一边（201；已拆 409；空通话/桥上现有两边/已在别的活动桥 409；未知通话 404；未知桥号 404）
GET  /bridges/{bridge_no}/swaps          这座桥历次换边的留痕（第几次、换哪边、换下/换上/不动各是谁、换边前的旧对齐）
POST /bridges/{bridge_no}/photos         桥还搭着时拍一张照（201，钉住此刻两边对到哪一对；已拆 409；未知桥号 404）
GET  /bridges/{bridge_no}/photos         这座桥拍过的全部照片（第几张、何时拍、拍时两边与逐对对齐）
GET  /bridges/{bridge_no}/photos/{n}     取这座桥的第 n 张照片（永远是拍时那份；没拍过/未知桥号 404）
GET  /sessions/{call_id}/bridges         该通话上过的全部桥（标明左/右；被换下去的带 currently_on_bridge=false；未知通话 404）
GET  /healthz                          健康检查
GET  /docs                             Swagger UI
```

会话视图关键字段：`status`、`content`（拼好的文本，一行一片段）、
`gaps`（当前缺口区间列表，如 `[[2,4]]`）、`was_incomplete`、`gap_history`
（每项含 `range`/`detected_at`/`filled_at`）、`version`、
`retransmissions`、`conflicts`、`completed_at`、`latest_draft`
（最近一稿的 `draft_no`/`issued_at`/`changed_since`，未发过稿为 `null`）、
`active_bridge`（这通电话此刻还搭着的桥：`bridge_no`/`side`(left|right)/
`other_call_id`/`created_at`，没搭桥或已拆为 `null`）。

稿（draft）关键字段：`draft_no`（稿号）、`draft_seq`（该通话第几稿）、
`status`/`content`/`gaps`（发稿那一刻的状态、正文、缺口）、`gap_history`
（截至当时的缺口历史）、`supersedes`（本稿订正的上一稿稿号）、
`predecessor_had_gaps`/`predecessor_gaps`（上一稿当时是否带缺口、缺在哪）、
`issued_at`、`is_withdrawn`/`withdrawn_at`（是否撤回、撤回时刻）、
`is_held`/`released`/`release_at`/`held_at`/`visibility`（是否仍压着、是否
已到点、解禁时刻、压稿时刻、`held`/`visible`）。稿一旦发出即冻结，撤回只
叠加状态；稿压着时正文、拼装单元、缺口、状态等字段一律为 `null`，到点后
同一稿号自动恢复为当时的正文和缺口，任何后续片段或撤回动作都不会改变其
正文和缺口。

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

认领（claim）关键字段：`draft_no`/`call_id`/`draft_seq`（认的是哪一稿）、
`status`（`claimed` 有人持有 / `unclaimed` 没人认）、`claimed_by`/`claimed_at`
（当前谁认着、何时认的，未认领为 `null`）、`history`（历次认领/交出记录：
谁认的、何时认、何时交出去，永不删除）、`draft`（当时那一稿的完整快照——
认领、交出、换人认都改不了它的正文和缺口）。没认领的稿签收、回放、撤回、
投递一律 409；认领记录只往 `claims` 表写，从不修改 `drafts`、`receipts`、
`playbacks`。

下游投递（delivery）关键字段：`draft_no`/`call_id`/`draft_seq`（投的是哪稿）、
`status`（`none` 还没投过 / `pending` 投出去待回音 / `accepted` 下游已收下 /
`returned` 下游已退回）、`current_attempt`/`attempt_count`（当前是第几次投、
共投过几次）、`attempts`（历次投递：`attempt`/`delivered_at`/`accepted_at`/
`returned_at`/`status`，退回后再投另起一行，永不删除）、`delivered_at`/
`accepted_at`/`returned_at`（最近一次投递的三个时刻）、`draft`（发稿当时的
完整快照——投递流转改不了它的正文和缺口）。没人认领不能投；待回音期间不能
再投；退了才能再投；收下是终态。投递只写 `deliveries` 表，从不修改 `drafts`。

勘误（errata）关键字段：`draft_no`/`call_id`/`draft_seq`（勘的是哪一稿）、
`seq`（对着哪一段——该稿快照里的片段序号）、`old_text`（那一稿当时该段的
原文，随记录钉住）、`new_text`（改成什么）、`issued_at`（出勘误的时刻）、
`draft`（当时那一稿的完整快照——勘误改不了它的正文和缺口）。没人认领不能
出；同一段不能出两次；缺口和越界序号不是段；撤过的稿不能出。勘误只写
`errata` 表，从不修改 `drafts`。

桥（bridge）关键字段：`bridge_no`（桥号，`B0001` 起全局递增）、
`left_call_id`/`right_call_id`（左右各是哪通电话——搭桥后不可直接改，只能
通过换边把其中一边换掉，桥号不变）、
`status`（`active` 搭着 / `dismantled` 已拆）、`created_at`/`dismantled_at`、
`left`/`right`（两边摘要：到了多少段、到过的最大序号、各自的缺口区间）、
`pairs`（逐对单元：对齐的是
`{kind:"aligned",seq,left:{seq,text},right:{seq,text}}`；缺口是
`{kind:"gap",missing:"left"|"right"|"both",seq?,gap?,left,right,marker}`，
缺的一边为 `null`，另一边的字带着但不顶替）、`aligned_count`/`gap_count`/
`total_pairs`、`aligned_up_to`（连续对齐前缀，"当时对齐到哪一对"）、
`gaps`（缺口区间列表）、`swap_count`/`swaps`（换过几次边及历次留痕：每项含
`swap_seq`/`side`/`old_call_id`/`new_call_id`/`other_call_id`/`swapped_at`/
`before`，`before` 是换边**前**旧两边身份与逐对对齐的完整快照，只插不改）、
`photo_count`/`photos`（桥还搭着时拍下的照片：每张含 `photo_seq` 这是这座桥
第几张、`taken_at` 何时拍、拍时的 `left_call_id`/`right_call_id` 与逐对对齐
`left`/`right`/`pairs`/`aligned_count`/`gap_count`/`total_pairs`/
`aligned_up_to`/`gaps`——只插不改，拍完后两通再来段、换边、拆桥都碰不到旧
照片；已拆桥不能再拍，但拆前的照片拆后仍在）。
活动桥这些对齐字段按当前两边片段**现算**；拆桥时把当时的全套值冻结进桥行，
之后两通再收段也不变。按通话列出时每项还带 `side`（该通最后在这座桥的左边
还是右边）与 `currently_on_bridge`（现行两边为 `true`，已被换下去为 `false`）。

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
# 发出去的稿要先有人认领，没认领不能签（409）
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'
# {"draft_no":"C1-D0001","status":"claimed","claimed_by":"张三",...}

# 稿一发出即进入待签；认领后下游按稿号签收
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
# 撤回也要先认领；C1-D0001 未签，拿稿号撤回
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'
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
# 听也要先认领；认了之后拿稿号开始听，从第 1 段顺序往后
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'
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
curl -X POST localhost:8000/drafts/C1-D0002/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'
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

### 先认领，再办事

```bash
# 稿发出去后，没人认领时：签、听、撤一律 409
curl -X POST localhost:8000/drafts/C1-D0001/receipt        # 409 draft not claimed

# 张三认领这一稿（稿号认到哪一稿、哪一通，一目了然）
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'
# {"draft_no":"C1-D0001","call_id":"C1","status":"claimed",
#  "claimed_by":"张三","claimed_at":"...","history":[...],"draft":{...当时那稿...}}

# 张三认着，李四认不走；张三自己重复认领幂等（200，时间不动）
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"李四"}'   # 409

# 张三把稿交出去，之后李四才能认；交出后没人认期间同样不能签/听/撤
curl -X POST localhost:8000/drafts/C1-D0001/claim/release
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"李四"}'   # 201

# 谁认过、何时认、何时交出去，全部留痕；那一稿的正文和缺口始终原样
curl localhost:8000/drafts/C1-D0001/claim
# {"status":"claimed","claimed_by":"李四",
#  "history":[{"claimed_by":"张三","claimed_at":"...","released_at":"..."},
#             {"claimed_by":"李四","claimed_at":"...","released_at":null}],...}

# 该通话全部稿的认领状态；拿一通的号认不到另一通的稿
curl localhost:8000/sessions/C1/claims
```

### 往下游投

```bash
# 没人认领的稿不能投（409）；先认领
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'

# 按稿号投：投完看得出投的是哪一稿（稿号、通话、第几稿、第几次投都在）
curl -X POST localhost:8000/drafts/C1-D0001/delivery
# {"draft_no":"C1-D0001","call_id":"C1","draft_seq":1,"status":"pending",
#  "current_attempt":1,"attempt_count":1,"attempts":[{"attempt":1,...}],
#  "draft":{...发稿当时那稿...}}

# 投出去还没回音，不能再投一次
curl -X POST localhost:8000/drafts/C1-D0001/delivery          # 409

# 下游可以退回；退了之后才能再投（新起一次，attempt=2，历次投递都留着）
curl -X POST localhost:8000/drafts/C1-D0001/delivery/return
curl -X POST localhost:8000/drafts/C1-D0001/delivery          # 201，attempt=2

# 下游也可以收下；收下之后不能再退、也不能再投
curl -X POST localhost:8000/drafts/C1-D0001/delivery/accept
curl -X POST localhost:8000/drafts/C1-D0001/delivery/return   # 409
curl -X POST localhost:8000/drafts/C1-D0001/delivery          # 409

# 投到哪了（重启后仍是这个状态）；投、退、再投、收都不改稿当时的正文和缺口
curl localhost:8000/drafts/C1-D0001/delivery
# {"status":"accepted","attempts":[{"attempt":1,"status":"returned",...},
#                                  {"attempt":2,"status":"accepted",...}],...}
curl localhost:8000/sessions/C1/deliveries                    # 该通话全部稿的投递状态
```

### 出勘误

```bash
# 没人认领的稿不能出勘误（409）；先认领
curl -X POST localhost:8000/drafts/C1-D0001/claim \
     -H 'Content-Type: application/json' -d '{"claimed_by":"张三"}'

# 按稿号对第 2 段出勘误：看得出勘的是哪一稿、对着哪一段、改成什么
curl -X POST localhost:8000/drafts/C1-D0001/errata \
     -H 'Content-Type: application/json' -d '{"seq":2,"new_text":"听不清"}'
# {"draft_no":"C1-D0001","call_id":"C1","draft_seq":1,"seq":2,
#  "old_text":"听得到吗","new_text":"听不清","issued_at":"...",
#  "draft":{...当时那一稿...}}

# 同一段不能出两次（409，原记录时间不动）；缺口不是段，不能对着缺口出
curl -X POST localhost:8000/drafts/C1-D0001/errata \
     -H 'Content-Type: application/json' -d '{"seq":2,"new_text":"又改"}'   # 409

# 那一稿当时的正文和缺口一个字不变；勘误单独可查
curl localhost:8000/drafts/C1-D0001          # 正文、缺口仍是发稿时的样子
curl localhost:8000/drafts/C1-D0001/errata   # 这一稿出过的全部勘误
curl localhost:8000/sessions/C1/errata       # 该通话出过的全部勘误（重启后还在）
```

### 压到点才见

```bash
# 稿照常发出（此刻即 INSERT-only 快照落库），再写明几点几分解禁
curl -X POST localhost:8000/sessions/C1/drafts            # C1-D0001
curl -X POST localhost:8000/drafts/C1-D0001/hold \
     -H 'Content-Type: application/json' \
     -d '{"release_at":"2026-09-11T14:00:00Z"}'
# 201：{"draft_no":"C1-D0001","is_held":true,"released":false,
#       "visibility":"held","release_at":"2026-09-11T14:00:00Z",
#       "content":null,"parts":null,"gaps":null,"status":null,...}

# 没到点：只能知道还压着、何时解，正文和缺口全是 null
curl localhost:8000/drafts/C1-D0001
# {"visibility":"held","release_at":"...","content":null,"gaps":null,...}

# 一稿只能压一次（改时刻/提前解都 409）；解禁时刻早于发稿时刻 422
curl -X POST localhost:8000/drafts/C1-D0001/hold \
     -H 'Content-Type: application/json' -d '{"release_at":"2026-09-11T13:00:00Z"}'
# 409 draft already held

# 到点后无需任何操作，同一稿号再拿就是发稿当时的正文和缺口
curl localhost:8000/drafts/C1-D0001
# {"visibility":"visible","is_held":false,"content":"...当时的正文...","gaps":[...]}

# 同一通电话后发的稿不能更早解（C1-D0002 的 release_at 早于 D0001 → 409）；
# 另一通电话各压各的，互不约束；重启后仍按钟点判，没到点继续看不见。
```

### 两通电话搭一座桥

```bash
# 两通电话各收到一些片段（C1 缺第 3 段，C2 四段齐）
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":1,"text":"你好"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":2,"text":"听得到吗"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":4,"text":"先这样"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C2","seq":1,"text":"喂"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C2","seq":2,"text":"听得到"}'
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C2","seq":3,"text":"我这边信号差"}'

# 搭成一座桥：逐对给，第 3 对只到右边 → 缺口“缺左”，右边的字不拿来顶左边
curl -X POST localhost:8000/bridges -H 'Content-Type: application/json' \
     -d '{"left_call_id":"C1","right_call_id":"C2"}'
# {"bridge_no":"B0001","status":"active","aligned_count":3,"gap_count":1,
#  "total_pairs":4,"aligned_up_to":2,"gaps":[[3,3]],
#  "pairs":[{"kind":"aligned","seq":1,"left":{"seq":1,"text":"你好"},
#            "right":{"seq":1,"text":"喂"}}, ...,
#           {"kind":"gap","missing":"left","seq":3,"left":null,
#            "right":{"seq":3,"text":"我这边信号差"},
#            "marker":"[桥缺口:第3对 缺左]"}, ...]}

# 同一通不能同时待在两座桥里（409）；空通话、自己跟自己搭也不行
curl -X POST localhost:8000/bridges -H 'Content-Type: application/json' \
     -d '{"left_call_id":"C1","right_call_id":"C3"}'      # 409 / 404

# 缺的那边补上：活动桥再看，同一座桥自动多对上一对（aligned_up_to=4）
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"C1","seq":3,"text":"等一下"}'
curl localhost:8000/bridges/B0001
# {"aligned_count":4,"gap_count":0,"aligned_up_to":4,"gaps":[]}

# 拆桥：两通恢复各自独立（片段一个不删），桥当时对齐到哪一对冻结留痕
curl -X POST localhost:8000/bridges/B0001/dismantle
# {"bridge_no":"B0001","status":"dismantled","aligned_count":4,
#  "dismantled_at":"...", "pairs":[...拆那一刻的样子...]}
curl localhost:8000/bridges/B0001       # 以后永远是拆时那份快照
curl localhost:8000/sessions/C1/bridges # 这通上过的桥（含左/右侧）
curl localhost:8000/bridges             # 全部桥（搭着的 + 拆掉的）
```

### 桥还搭着时换一边

```bash
# C1-C2 的桥还搭着，第三通 C3 已有片段；把左边从 C1 换成 C3
curl -X POST localhost:8000/bridges/B0001/swap -H 'Content-Type: application/json' \
     -d '{"side":"left","call_id":"C3"}'
# {"bridge_no":"B0001","status":"active",
#  "left_call_id":"C3","right_call_id":"C2",   # 还是这座桥，现行两边是新的
#  "aligned_count":...,"pairs":[...按 C3-C2 重新对齐...],
#  "swap_count":1,"swaps":[{"swap_seq":1,"side":"left",
#    "old_call_id":"C1","new_call_id":"C3","other_call_id":"C2",
#    "swapped_at":"...","before":{...换边前 C1-C2 的完整对齐快照...}}]}

# C1 被换下去，恢复自由：active_bridge 清空，可以立刻再搭别的桥
curl localhost:8000/sessions/C1          # "active_bridge":null

# 换边当时旧两边是谁、对到哪，单独留痕，再换边、拆桥都盖不掉
curl localhost:8000/bridges/B0001/swaps
# {"bridge_no":"B0001","swap_count":1,"swaps":[
#   {"swap_seq":1,"side":"left","old_call_id":"C1","new_call_id":"C3",
#    "other_call_id":"C2","swapped_at":"...",
#    "before":{"left":{"call_id":"C1",...},"right":{"call_id":"C2",...},
#              "pairs":[...],"aligned_up_to":2,"gaps":[[3,3]]}}]}

# 已拆掉的桥不能再换边（409）；空通话（409）、已在别的活动桥里的通话（409）、
# 桥上现有的两边（409）都换不上来；未知桥号/通话 404。
```

### 桥还搭着时把此刻的对齐拍下来

```bash
# B0001 还搭着：A 到两段、B 到一段（第 2 对缺右）。拍第一张
curl -X POST localhost:8000/bridges/B0001/photos
# {"bridge_no":"B0001","photo_seq":1,"taken_at":"...",
#  "left_call_id":"A","right_call_id":"B",
#  "aligned_count":1,"aligned_up_to":1,"gaps":[[2,2]],
#  "pairs":[{"kind":"aligned","seq":1,...},
#           {"kind":"gap","missing":"right","seq":2,"right":null,...}], ...}

# 拍完之后缺的那边补上：活动桥的现行对齐继续现算（对上 2 对）
curl -X POST localhost:8000/fragments -H 'Content-Type: application/json' \
     -d '{"call_id":"B","seq":2,"text":"b2"}'
curl localhost:8000/bridges/B0001            # aligned_up_to=2, photo_count=1

# 再拍一张：看得出这是第 2 张、当时对上 2 对
curl -X POST localhost:8000/bridges/B0001/photos   # photo_seq=2, aligned_up_to=2

# 第 1 张永远是拍那一刻的样子 —— 后来补的段碰不到它
curl localhost:8000/bridges/B0001/photos/1
# {"photo_seq":1,"aligned_up_to":1,"gaps":[[2,2]],
#  "right":{"call_id":"B","fragment_count":1,...}, ...}   # 拍时 B 只有 1 段
curl localhost:8000/bridges/B0001/photos      # 这座桥拍过的全部照片（按第几张）

# 拍照不改两通各自的会话，也不动桥上正在算的对齐；拆桥之后不能再拍，
# 但拆前拍过的照片照样可查（重启后也在）。
curl -X POST localhost:8000/bridges/B0001/dismantle
curl -X POST localhost:8000/bridges/B0001/photos     # 409 bridge already dismantled
curl localhost:8000/bridges/B0001/photos             # 旧照片仍在
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
tests/           167 个测试：乱序、重传、通话隔离、缺口（含越界/收缩/无结束标记
                 补齐/大空洞）、旧表迁移、重启持久化，已发稿的钉住、订正链、
                 两通电话隔离、重启后稿不丢，签收（待签钉住、按号签收、
                 不串签、不重复签、新稿不顶旧待签、重启后待签还在、老库补单），
                 回放（顺序收听、缺口停住不跳过、补段不让旧稿变完整、
                 新旧稿两条回放独立、两通电话不串、重启停在原处、不改稿不签收），
                 撤回（按号撤回、快照不改正文缺口、已签不可撤、撤后不可签、
                 正在听停在原处、两通电话不串、重启后仍是撤回态），
                 迟到片段（单独记录、看得出补在哪一稿后面、不改稿不动待签
                 不让回放突然听完、两通电话不串、重启后记录还在），
                 以及认领（没认领不能签/听/撤/投、认了别人认不走、本人重复认领
                 幂等、交出去才能换人认、认领不改稿、两通电话不串、
                 重启后认了谁还在），下游投递（按稿号投、看得出投的是哪稿、
                 没认领不能投、没回音不能再投、收下终态不能再退再投、退了才能
                 再投且历次留痕、投递不改正文缺口、两通电话不串、重启后投到哪
                 了还在、与撤回互斥），勘误（按稿号对段出、看得出哪稿哪段改成
                 什么、不改那一稿正文缺口、没认领不能出、同一段不能出两次、
                 缺口不是段、撤过不能出、已签已投照样能出、两通电话不串、
                 重启后勘误还在），定时压稿（没到点只见“还压着、何时解”、
                 正文缺口全遮、到点无需操作自动可见、解禁不早于发稿、写定不改
                 不提前解、一稿只能压一次、后发稿不更早解、压着时回放/投递/
                 勘误都被拦住且各视图不泄正文、两通电话不串、重启后按钟点判），
                 桥（逐对对齐、一边缺了就是缺口不拿另一边凑、缺段补上活动桥
                 自动多对上一对、空通话/自己跟自己不能搭、同一通不能同时待在
                 两座桥（含左右交叉，索引+触发器兜底）、拆后可再搭历史全留、
                 拆桥不碰两通片段、拆时对齐快照冻结、按通话列桥不串、重启后
                 活动桥还在且对齐对得上、拆过的桥仍是拆时快照），
                 换边（桥号不变按新两边重新对齐、换下的恢复自由且片段不动、
                 日后可再换回来、空通话/桥上现有两边/已在别的活动桥/已拆的桥
                 都换不上、换边只插 bridge_swaps 并 UPDATE 桥列不碰片段、
                 每次换边前旧两边身份与旧对齐快照留痕且不被新两边/拆桥盖掉、
                 按通话列得出换下去的经过、触发器兜底、重启后现行对齐与历次
                 留痕都还在），
                 桥拍照（搭着时能拍、钉住此刻两边对到哪一对、同一座可拍多张
                 各带第几张、拍完再来新段旧照片不变而现行对齐继续现算、拍照不
                 改两通会话也不动桥上现行对齐、已拆的桥不能再拍但拆前照片仍在、
                 换边后旧照片不被新两边盖掉、两桥各自编号不串、重启后照片还在）
Dockerfile / docker-compose.yml
```

## 已知边界

- `call_id` 必须由上游保证唯一标识一次通话；若链路会复用 call_id，
  需要在上游拼接链路口令后再送入。
- 越过已知 `is_last` 序号的片段、矛盾的结束标记会照收并计入
  `conflicts`，由人工介入处理。
- 单实例服务；多副本部署需要把存储换成共享数据库（表结构可直接平移
  到 PostgreSQL）。
