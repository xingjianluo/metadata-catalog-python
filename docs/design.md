# 元数据目录与血缘发现服务 — 设计说明

## 1. 元数据模型

| 对象 | 字段 | 说明 |
|---|---|---|
| `Entity`（实体） | `id, name, kind, fields, tags, owner, version, created_snapshot, updated_snapshot, retired_snapshot` | `kind ∈ {TABLE, VIEW, STREAM}`；`fields` 为有序元组；`tags` 为无序集合；`retired_snapshot` 非空即已退役；所有模型不可变（frozen dataclass）。 |
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
- 快照视图 `SnapshotView(snapshot_id, entities, edges)` 不可变、惰性构建并缓存：
  - 实体取"快照号不超过当前快照的最近版本"，且**已退役实体不出现在视图中**；
  - 边按生命周期区间 `[created_snapshot, revoked_snapshot)` 判定，
    只包含在该快照生效的边，并按边 id 升序排列。
- 任何分析/查询都可以指定 `snapshot_id`，在旧快照上复现历史结论；
  新提交绝不改变已返回的旧视图。

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
  - 五元组重复：幂等返回，`created=false`
- 直接展开：`upstream / downstream`，邻居按 `(实体 id, 列名)` 排序。
- 全链影响分析 `impact_analysis`：
  - 在**固定快照**上计算，输出节点与环都有序、可复现；
  - 遍历用当前路径栈检测回到祖先的边：**环路显式标记**
    （`cycle=true` 且给出完整 `cycle_path`），不会被静默截断或误报为普通节点；
  - 非环节点只输出一次，环以独立条目报告。
- 带可见性的版本 `scoped_impact_analysis`：先校验起点读权限，
  再只沿"两端实体均对该用户可见"的边展开，结果不泄露无权实体。

## 4. 可见性判定规则

对任一固定快照，用户对实体的可见性按以下顺序判定：

1. `is_admin=true`：全部可见；
2. 用户是实体 `owner`：可见；
3. 用户被授予 owner 级读权限（`PUT /owner-access/{user}`）：该授权快照后全部可见；
4. 用户被逐实体显式授权（`PUT /grants/{entity}`）：仅该实体可见；
5. 其余：不可见。

关键原则：

- **越权不等于不存在。** 实体存在但无权限 → `403 permission_denied`；
  实体不存在 → `404 not_found`。两者原因码不同，绝不以空结果掩盖越权。
- 授权本身也带快照号，历史快照上的授权结论可复现。
- 血缘边仅当其两端实体均可见时才对用户可见，
  用户无法通过血缘关系推断无权实体的存在。

## 5. 退役与撤销

### 实体退役（`POST /entities/{id}/retire`）

| 情况 | 处理 | reason |
|---|---|---|
| 实体从未登记 | 拒绝 `404` | `not_found` |
| 实体仍挂有**未撤销**的血缘边 | 拒绝 `409`，状态不变 | `entity_in_use_by_lineage` |
| 重复退役（已退役） | 幂等返回当前退役版本，`retired=false`，不产生新快照 | — |
| 正常退役 | 提交带 `retired_snapshot` 的新版本，`retired=true` | — |

退役生效后的行为：

- 该快照起实体退出快照视图：按标签/归属的发现结果不再包含它，
  也不再作为上游/下游参与血缘展开；
- 对已退役实体的变更操作（重登记、加列、删列、以其为端点建血缘、授权）
  一律拒绝 `410 entity_retired`；
- 历史快照不受影响：退役前的快照上实体仍在役，旧查询结论逐位可复现。

### 血缘撤销（`POST /lineage/revoke`）

| 情况 | 处理 | reason |
|---|---|---|
| 该五元组从未建立过 | 拒绝 `404` | `not_found` |
| 重复撤销（已撤销） | 幂等返回，`revoked=false`，不产生新快照 | — |
| 正常撤销 | 关闭当前生命周期区间，`revoked=true` | — |

**撤销后按同一五元组重建：确定地算重建生效。** 存储按生命周期区间
`[created_snapshot, revoked_snapshot)` 记录每条边；重建开启一段新区间，
自重建快照起该边重新参与展开，撤销与重建之间的快照上它确定地不存在。
任一快照下边的存在性唯一确定，绝不出现"一会儿存在一会儿不存在"的摇摆。

### 对历史快照与既有结论的兼容范围

- 退役与撤销都只是**新提交**：任何已返回的快照号上的视图、影响分析、
  分页结果保持逐位可复现，新旧行为对历史快照零影响。
- 已退役实体在旧快照上仍可读、仍参与展开；已撤销边在旧快照上仍生效。
- 拒绝理由三方可区分：从未登记 `404 not_found`、无权限
  `403 permission_denied`、已退役 `410 entity_retired`。
  判定顺序固定为存在性 → 权限 → 退役状态，无权用户无法通过原因码
  探测实体的退役状态。
- 并发交错（建边/撤边/退役/查询）由同一把锁串行化"检查—判断—写入"：
  退役与建边冲突时恰有一方成功，失败方以
  `entity_in_use_by_lineage` 或 `entity_retired` 拒绝；
  任何快照都不会出现"生效边引用已退役实体"的悬空状态。

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
| 实体/字段不存在 | 404 | `not_found` |
| 越权访问 | 403 | `permission_denied` |
| 实体已退役 | 410 | `entity_retired` |
| 标识冲突、演进拒绝、血缘拒绝、退役被血缘阻断 | 409 | 对应领域原因码 |

错误响应体统一为：
`{"error": <异常类名>, "reason": <原因码>, "message": <描述>}`。
