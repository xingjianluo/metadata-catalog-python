# 元数据目录与血缘发现服务 — 设计说明

## 1. 元数据模型

| 对象 | 字段 | 说明 |
|---|---|---|
| `Entity`（实体） | `id, name, kind, fields, tags, owner, version, created_snapshot, updated_snapshot` | `kind ∈ {TABLE, VIEW, STREAM}`；`fields` 为有序元组；`tags` 为无序集合；所有模型不可变（frozen dataclass）。 |
| `FieldSpec`（字段） | `name, type` | `type ∈ {STRING, INTEGER, BOOLEAN, DOUBLE, TIMESTAMP}`。 |
| `LineageEdge`（血缘边） | `id, level, source_entity/column, target_entity/column, transformation, created_snapshot` | `level ∈ {TABLE, COLUMN}`；`id` 由五元组确定性派生。 |
| `User`（使用者） | `name, is_admin` | HTTP 场景由 `X-User` / `X-User-Admin` 头给出。 |

血缘边 ID 规则（保证幂等）：

- 表级：`edge:TABLE:<src>-><dst>`
- 列级：`edge:COLUMN:<src>.<srcCol>-><dst>.<dstCol>`

### 状态与并发模型

- 单一内存存储 `InMemoryStore`，一把可重入锁把一次"检查—判断—写入"
  整体串行化为一个**原子提交**；因此并发提交不会出现半更新状态。
- 每次提交由单调逻辑时钟分配一个严格递增的**快照号**。
- 快照视图 `SnapshotView(snapshot_id, entities, edges, retired_entities)`
  不可变、惰性构建并缓存：
  - 实体取"快照号不超过当前快照的最近版本"；
  - 边只含该快照下**活跃**的边：边的生命周期以事件流
    `[(快照号, 活跃/撤销)]` 记录，某快照下的活跃性由"不超过该快照号的
    最后一条事件"决定；撤销/重建只追加事件，不回改历史；
  - `retired_entities` 为该快照下已退役（退役快照号 ≤ 当前快照）的实体集合。
- 任何分析/查询都可以指定 `snapshot_id`，在旧快照上复现历史结论；
  新提交（含退役与撤销）绝不改变已返回的旧视图。

## 2. 结构演进与血缘兼容规则

### 登记（`PUT /entities/{id}`）

| 情况 | 处理 | reason |
|---|---|---|
| id/name/owner 为空或非字符串 | 拒绝 `400` | `invalid_identifier` |
| kind 非法 | 拒绝 `400` | `invalid_kind` |
| 字段列表出现重名/空名/非 FieldSpec | 拒绝 `400` | `invalid_field` |
| 同 id 但 name/kind/owner/fields/tags 有差异 | 拒绝 `409` | `identifier_conflict` |
| 定义完全一致的重复登记 | 幂等返回既有实体，`created=false`，不产生新版本 | — |

失败一律显式拒绝，绝不静默跳过。

### 演进

| 操作 | 规则 | reason |
|---|---|---|
| 新增字段 `add_field` | 允许。新字段**追加在字段表末尾**，不移动既有列位置；`version + 1` | — |
| 新增同名字段且同类型 | 幂等无变化（`changed=false`） | — |
| 新增同名字段但类型不同 | 拒绝 `409` | `field_type_conflict` |
| 删除字段 `drop_field` | 仅当该列**没有任何列级血缘引用**时允许，`version + 1`；否则拒绝 | `field_in_use_by_lineage` |
| 修改字段类型 `change_field_type` | 一律拒绝；需要新类型时应新增字段，避免既有下游按旧类型读取而静默错位 | `type_change_not_allowed` |
| 实体不存在 / 字段不存在 | 拒绝 `404` | `not_found` |

**为什么既有血缘不会错位：** 列级血缘以 `(实体 id, 列名)` 为锚点，
而不是列下标；新增字段只追加（锚点不变），删列在有血缘引用时被阻断，
改类型被完全禁止。因此任何被允许的演进行为都不会让既有血缘静默指向另一列。

## 3. 血缘与影响分析

- 建边时校验两端实体存在；列级边还要求两端列存在，且级别与列参数匹配。
  - 级别/列参数不匹配：`level_column_mismatch`
  - 列不存在：`column_not_found`
  - 实体不存在：`not_found`
  - 自环（源与目标完全相同）：`self_lineage`
  - 五元组重复且仍活跃：幂等返回，`created=false`
  - 任一端实体已退役：`entity_retired`（撤销后重建见第 4 节）
- 直接展开：`upstream / downstream`，邻居按 `(实体 id, 列名)` 排序，
  已撤销的边与已退役的实体不参与展开。
- 全链影响分析 `impact_analysis`：
  - 在**固定快照**上计算，输出节点与环都有序、可复现；
  - 遍历用当前路径栈检测回到祖先的边：**环路显式标记**
    （`cycle=true` 且给出完整 `cycle_path`），不会被静默截断或误报为普通节点；
  - 非环节点只输出一次，环以独立条目报告。
- 带可见性的版本 `scoped_impact_analysis`：先校验起点读权限，
  再只沿"两端实体均对该用户可见"的边展开，结果不泄露无权实体。

## 4. 退役与撤销

### 实体退役（`POST /entities/{id}/retire`）

| 情况 | 处理 | reason |
|---|---|---|
| 实体从未登记 | 拒绝 `404` | `not_found` |
| 实体仍挂有活跃（未撤销）血缘 | 拒绝 `409`，须先撤销全部血缘，绝不留下悬空关系 | `entity_has_active_lineage` |
| 首次退役 | 提交一个退役事件（新快照号），返回 `retired=true` | — |
| 重复退役 | 幂等返回 `retired=false` 与原退役快照号，不产生新提交、不改状态 | — |

退役后的效果（均按快照判定，历史快照不受影响）：

- 不再出现在按标签/归属的发现结果里；
- 不再作为可用的上游/下游参与展开（`upstream`/`downstream`/影响分析
  以其为起点时拒绝 `entity_retired`）；
- 直查已退役实体返回 `410 entity_retired`，与"从未登记"（`404 not_found`）、
  "无权限"（`403 permission_denied`）三类拒绝理由明确区分；
- 退役是**终态**：同 id 的重新登记、字段演进一律拒绝 `entity_retired`，
  不存在"复活"路径，语义确定稳定。

### 血缘撤销（`POST /lineage/revoke`）

| 情况 | 处理 | reason |
|---|---|---|
| 边从未登记 | 拒绝 `404` | `not_found` |
| 首次撤销 | 追加一条"撤销"生命周期事件（新快照号），返回 `revoked=true` | — |
| 重复撤销 | 幂等返回 `revoked=false` 与原撤销快照号，不产生新提交 | — |
| 撤销后按同一五元组重建 | **重建生效**：自新快照起再次参与展开（`created=true`） | — |

关键语义：

- 撤销只追加事件、不删边定义。撤销**之前**的快照上，影响分析/展开/
  发现的既有结论**逐位不变、可原样复现**；撤销**之后**的新快照里，
  该边不再参与上游/下游展开。
- "撤销后重建"的语义是确定且稳定的：**重建生效**。每个快照各有确定答案
  ——创建后、撤销后、重建后三个区间的快照分别看到"活跃/已撤销/活跃"，
  不存在"一会儿算存在、一会儿不算"的摇摆。
- 建边/撤边/退役/查询并发交错时，所有"检查—判断—写入"在同一事务锁内
  串行提交：同一实体上"退役"与"建边"竞争时恰好一方成功，失败方拿到
  可区分原因（`entity_has_active_lineage` 或 `entity_retired`）；
  任何快照都不会出现"活跃边指向已退役实体"或半更新的中间态。

### 对历史快照与既有查询结论的兼容范围

- 退役与撤销都是**只追加**的提交，不修改任何已提交的实体版本、边定义
  或生命周期事件；因此凡是带 `snapshot_id` 的查询（影响分析、直接展开、
  发现分页、直查实体），在退役/撤销发生前的快照上重放，结论与当时
  逐位一致。
- 兼容边界：快照号小于退役/撤销快照号的一切结论完全保留；
  大于等于退役/撤销快照号的新查询按新状态计算。

## 5. 可见性判定规则

对任一固定快照，用户对实体的可见性按以下顺序判定：

1. `is_admin=true`：全部可见；
2. 用户是实体 `owner`：可见；
3. 用户被授予 owner 级读权限（`PUT /owner-access/{user}`）：该授权快照后全部可见；
4. 用户被逐实体显式授权（`PUT /grants/{entity}`）：仅该实体可见；
5. 其余：不可见。

关键原则：

- **越权不等于不存在，退役也不等于不存在。** 实体存在但无权限 →
  `403 permission_denied`；实体不存在 → `404 not_found`；实体已退役 →
  `410 entity_retired`。三者原因码两两不同，绝不以空结果掩盖越权。
- 授权本身也带快照号，历史快照上的授权结论可复现。
- 血缘边仅当其两端实体均可见时才对用户可见，
  用户无法通过血缘关系推断无权实体的存在。

## 6. 发现与分页

`POST /entities/query` 支持按标签（任一命中）、归属精确匹配过滤，
并提供**快照钉住的键集分页**：

- 首次查询取当前快照号，随结果返回；下一页 token 内嵌同一快照号与最后一条 id
  （`base64("snapshot:last_id")`）。
- 整轮翻页都在同一快照上执行；翻页期间的并发写入既不会造成重复，
  也不会造成遗漏，要看到新数据需不带 token 重新发起查询。
- 通过"多取一条"判断是否还有下一页，末页恰好等于 `limit` 时不会产生多余 token。
- token 被篡改 → `invalid_page_token`；token 快照与显式 `snapshot_id` 不一致 →
  `snapshot_mismatch`。

## 7. HTTP 错误码映射

| 场景 | HTTP 状态 | reason |
|---|---|---|
| 参数/请求头非法、token 非法 | 400 | `invalid_*` / `missing_user` |
| 实体/字段/血缘不存在 | 404 | `not_found` |
| 越权访问 | 403 | `permission_denied` |
| 目标实体已退役 | 410 | `entity_retired` |
| 标识冲突、演进拒绝、血缘拒绝、退役被活跃血缘阻断 | 409 | 对应领域原因码（如 `entity_has_active_lineage`） |

错误响应体统一为：
`{"error": <异常类名>, "reason": <原因码>, "message": <描述>}`。
