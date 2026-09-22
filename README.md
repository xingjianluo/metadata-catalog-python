# 本地元数据目录与血缘发现服务

一个基于 FastAPI 的**本地内存**元数据目录，模拟元数据实体登记、结构演进、
表级/列级血缘、影响分析与基于标签和归属的可见性控制。所有写入以
**不可变版本快照**原子提交，所有读取基于固定快照，因此并发写入与查询交错时
结论可预期、可复现。

## 1. 元数据模型

| 对象 | 关键字段 | 说明 |
| --- | --- | --- |
| `Entity` | `id`、`name`、`owner`、`columns`、`tags`、`created/updated_snapshot` | `id` 为实体主键；`name`/`owner` 为**身份属性**；`owner` 对自己的实体隐式可见。 |
| `ColumnDef` | `name`、`type`、`nullable`、`default` | 列名是列的稳定标识；支持 STRING/INTEGER/BOOLEAN/FLOAT/TIMESTAMP。 |
| `LineageEdge` | `(source_entity, target_entity[, source_col, target_col])` | 两端列名为空即**表级血缘**，否则为**列级血缘**。 |
| `Snapshot` | `version`、`entities`、`edges`、`acl` | 每次提交生成一个单调递增的新版本，历史快照保留。 |

## 2. 实体登记与结构演进规则

对同一 `id` 重复登记：

1. **完全相同 → 幂等**：不产生新版本，返回原快照版本（`evolved=false`）。
2. **身份冲突 → 拒绝 `SCHEMA_IDENTITY_CONFLICT`**：`name` 或 `owner`
   与既有值不同。标签（`tags`）不是身份属性，重复登记时与既有标签取并集。
3. **字段缺失 → 拒绝 `SCHEMA_MISSING_FIELD`**：重复登记遗漏了既有列。
   **不允许删字段**，避免既有血缘与检索静默错位。
4. **重排/删除列 → 拒绝 `SCHEMA_DROP_FORBIDDEN`**：列只能在**尾部追加**，
   既有列的位置（顺序）保持稳定。
5. **类型不匹配 → 拒绝 `SCHEMA_TYPE_MISMATCH`**：同名列的 `type` 或
   `nullable` 一经登记不可改变。
6. **同次登记列名重复 → 拒绝 `SCHEMA_DUPLICATE_FIELD`**。
7. **新增列缺省语义确定且稳定**：
   - 可空列直接允许追加；历史数据回填的缺省值为
     `default`（若显式给出）否则按类型确定：INTEGER→`0`、FLOAT→`0.0`、
     BOOLEAN→`false`、STRING/TIMESTAMP→`null`（见
     `ColumnType.default_default`）。
   - 新增 **NOT NULL** 列必须显式给出 `default`，否则拒绝
     `NEW_COLUMN_NO_DEFAULT`。

任何字段问题都不会被静默跳过：响应体携带机器可判定的 `reason` 与
`context`（HTTP 层除 404/403/400 外统一返回 422）。

## 3. 血缘与兼容规则

- 血缘边按 key `(source, target, source_col, target_col)` 去重；
  **完全相同的边重复登记幂等**。
- 同一对表端点不能同时存在表级与列级血缘，否则拒绝
  `LINEAGE_DUPLICATE_CONFLICT`（粒度冲突可区分）。
- 列级血缘要求两端列在当前快照中存在，否则 `COLUMN_NOT_FOUND`。
- **结构演进兼容**：只允许尾部加列，既有列名/类型/位置不变，因此旧的
  列级血缘边始终能解析，不会静默错位（见
  `test_evolution_keeps_existing_lineage_intact`）。
- 影响分析 `GET /entities/{id}/impact?direction=upstream|downstream`：
  - 列级边同时参与表级向上/向下展开；
  - **环路被明确标记**在 `cycles` 中（自环为单元素环，多跳环规范化为
    字典序最小起点表示），不做静默截断，环外可达节点照常返回；
  - `nodes`、`edges`、`cycles` 均按确定顺序排序，**同一快照下逐字节可复现**。

## 4. 可见性判定规则

- 调用者由请求头 `X-User` 标识（缺省 `anonymous`）。
- 可见集合 = `owner == user` 的实体 ∪ 通过
  `POST /entities/{id}/grants` 显式授权的实体。
- 列表/检索只在可见集合内按 `tag`、`owner` 过滤。
- **越权访问具体实体或血缘返回 403**（`UNAUTHORIZED_ENTITY` /
  `UNAUTHORIZED_LINEAGE`），而不是返回空结果；仅“目录列举”按可见性
  合法地裁剪为空列表。
- 影响分析只经过调用者两端均可见的边，不可见的下游不会泄露。

## 5. 并发一致性

- 写操作在同一把锁内完成“校验 + 原子替换 head 快照”，提交有序；
- 读操作进入时取快照引用，之后为纯函数计算：写入与查询交错时只可能看到
  提交前或提交后的**完整**状态，不存在半更新快照；
- 分页 cursor 为 base64 编码的 `{快照版本, 偏移, 过滤条件}`，翻页锁定首次
  查询的快照，因此并发写入下翻页**无重复、无遗漏**；cursor 与过滤条件
  不匹配返回 `INVALID_CURSOR`。

## 6. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 存活与当前 head 快照版本 |
| POST | `/entities` | 登记/幂等演进实体 |
| POST | `/entities/{id}/grants` | 授予用户可见权限 |
| POST | `/lineage` | 登记表级/列级血缘 |
| GET | `/lineage?source_entity=&target_entity=` | 查询两端点间血缘（越权 403） |
| GET | `/entities/{id}/impact?direction=upstream|downstream` | 影响分析 |
| GET | `/entities?tag=&owner=&limit=&cursor=&snapshot_version=` | 可见性过滤 + 稳定分页 |

## 7. 本地验证

需要 Python ≥ 3.14 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装依赖（含 pytest）
uv sync

# 运行全部单测（-s 可查看每个用例打印的 输入/快照标识/判定结果）
uv run pytest -q -s

# 启动服务
uv run uvicorn main:app --reload
```

快速手工验证：

```bash
curl -s -X POST localhost:8000/entities -H 'Content-Type: application/json' \
  -d '{"id":"orders","name":"orders","owner":"alice",
       "columns":[{"name":"id","type":"INTEGER","nullable":false}],"tags":["pii"]}'
curl -s 'localhost:8000/entities' -H 'X-User: alice'
```

## 8. 测试覆盖与日志

测试按主题分文件：

- `tests/test_schema_evolution.py`：幂等登记与全部结构演进拒绝原因；
- `tests/test_lineage.py`：血缘幂等、粒度冲突、列不存在、上/下游展开、
  自环与多跳环标记、演进后血缘不错位、同快照并发读可复现；
- `tests/test_visibility.py`：owner/grant 可见性、标签与归属过滤、
  越权 403、不可见边不泄露；
- `tests/test_concurrency.py`：并发写入下分页无重复/遗漏、过滤分页、
  旧快照重放一致、交错读写下无半更新快照、并发提交无丢失；
- `tests/test_api.py`：HTTP 状态码与可区分错误码。

每个判定点均有明确断言依据；`tests/helpers.log_case` 统一打印
`case=<用例> snapshot=<快照版本> verdict=<判定> detail=<输入与上下文>`，
拒绝用例打印具体 `reason` 与 `context`。
