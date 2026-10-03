# metadata-catalog-python-bootstrap

本地元数据目录与血缘发现服务：用本地内存状态模拟元数据实体、血缘关系与访问控制，
使**并发写入**与**不同可见性**下的查询结论可预期、可复现。

- 实体登记幂等，非法/冲突登记以可区分原因码拒绝；结构演进只允许"追加字段"等兼容操作。
- 表级/列级血缘双向展开；环路显式标记不截断；影响分析绑定快照、顺序稳定、可复现。
- 实体可退役、血缘可撤销：均幂等、只追加不改历史；退役被活跃血缘阻断，
  撤销后按同一定义重建确定生效（但重建会重放首次建边的端点校验，
  列已删/实体已退役则按首次相同的原因码拒绝）；
  三类拒绝理由（未登记/已退役/无权限）明确区分，且先查权限再查退役。
- 标签/归属发现 + 键集分页，快照钉住，并发写入下翻页不重不漏。
- 基于管理员/归属/显式授权的可见性；越权返回 `403 permission_denied` 而不是空结果。

## 目录结构

```
metadata_catalog/
  models.py       领域模型（实体/字段/血缘边/使用者）
  clock.py        单调逻辑时钟（快照号）
  store.py        线程安全存储 + 不可变快照视图（MVCC 本地简化版）
  catalog.py      实体登记与结构演进
  lineage.py      血缘建立、展开、环路感知的影响分析
  visibility.py   授权与可见性判定
  discovery.py    标签/归属过滤 + 快照键集分页
  service.py      门面装配
  api.py          FastAPI HTTP 适配层
tests/            单元/并发测试（含输入、快照标识、判定结果日志）
docs/design.md    元数据模型、演进/兼容规则、可见性规则详述
main.py           ASGI 入口
```

## 本地验证

需要 Python 3.14+，使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
uv sync                                   # 安装依赖（含 pytest 开发依赖）
uv run pytest -v                          # 运行全部测试（57 个用例）
uv run pytest -s                          # 同时打印每个判定的输入/快照/结论日志
uv run pytest -s tests/test_retirement_revocation.py   # 只看退役/撤销用例及其判定日志
uv run pytest -s tests/test_terminal_state_and_rebuild.py
# ^ 只看本轮修复的三类用例：撤销后删列再重建被拒、退役实体不可授权且
#   全入口不可见、无权查询退役实体的拒绝优先级、并发交错无悬空引用
uv run python -m compileall metadata_catalog   # 纯编译检查
```

启动 HTTP 服务：

```bash
uv run fastapi dev main.py                # http://127.0.0.1:8000  （/docs 为 Swagger）
```

### curl 示例

```bash
# 登记实体（owner=alice）
curl -s -X PUT http://127.0.0.1:8000/entities/t1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"T1","kind":"TABLE","fields":[{"name":"a","type":"STRING"}],"tags":["pii"],"owner":"alice"}'

curl -s -X PUT http://127.0.0.1:8000/entities/t2 \
  -H 'Content-Type: application/json' \
  -d '{"name":"T2","kind":"TABLE","fields":[],"owner":"bob"}'

# 建立表级血缘 t1 -> t2
curl -s -X PUT http://127.0.0.1:8000/lineage -H 'Content-Type: application/json' \
  -d '{"source_entity":"t1","target_entity":"t2","level":"TABLE"}'

# 越权：bob 读 alice 的实体 => 403 permission_denied
curl -s -i http://127.0.0.1:8000/entities/t1 -H 'X-User: bob'

# owner 访问；影响分析（管理员视角）
curl -s http://127.0.0.1:8000/entities/t1 -H 'X-User: alice'
curl -s 'http://127.0.0.1:8000/entities/t1/impact?direction=downstream' \
  -H 'X-User: root' -H 'X-User-Admin: true'

# 快照钉住的分页查询
curl -s -X POST http://127.0.0.1:8000/entities/query \
  -H 'Content-Type: application/json' -H 'X-User: root' -H 'X-User-Admin: true' \
  -d '{"limit":10}'

# 撤销血缘 t1 -> t2（幂等；撤销前的快照结论不受影响）
curl -s -X POST http://127.0.0.1:8000/lineage/revoke \
  -H 'Content-Type: application/json' \
  -d '{"source_entity":"t1","target_entity":"t2","level":"TABLE"}'

# 退役实体（仍有活跃血缘时会被 409 entity_has_active_lineage 拒绝）
curl -s -X POST http://127.0.0.1:8000/entities/t2/retire

# 退役后直查 => 410 entity_retired（区别于 404 not_found / 403 permission_denied）
# 注意拒绝优先级：先权限后退役——无权用户（如 bob）查退役实体只会得到
# 403 permission_denied，不会泄露"该实体存在过"；有权用户才看到 410
curl -s -i http://127.0.0.1:8000/entities/t2 -H 'X-User: alice'

# 对已退役实体授权 => 409 entity_retired（退役是终态，授权不能复活它）
curl -s -i -X PUT http://127.0.0.1:8000/grants/t2 \
  -H 'Content-Type: application/json' -d '{"user":"carol"}'

# 撤销列级血缘 -> 删列 -> 按原定义重建 => 409 column_not_found
# （与首次对不存在的列建边理由相同；任何快照都不会出现悬空活跃血缘）
curl -s -X PUT http://127.0.0.1:8000/entities/t3 \
  -H 'Content-Type: application/json' \
  -d '{"name":"T3","kind":"TABLE","fields":[{"name":"x","type":"STRING"}],"owner":"alice"}'
curl -s -X PUT http://127.0.0.1:8000/lineage -H 'Content-Type: application/json' \
  -d '{"source_entity":"t1","target_entity":"t3","level":"COLUMN","source_column":"a","target_column":"x"}'
curl -s -X POST http://127.0.0.1:8000/lineage/revoke -H 'Content-Type: application/json' \
  -d '{"source_entity":"t1","target_entity":"t3","level":"COLUMN","source_column":"a","target_column":"x"}'
curl -s -X DELETE http://127.0.0.1:8000/entities/t3/fields/x
curl -s -i -X PUT http://127.0.0.1:8000/lineage -H 'Content-Type: application/json' \
  -d '{"source_entity":"t1","target_entity":"t3","level":"COLUMN","source_column":"a","target_column":"x"}'
```

## 关键规则速查

| 关注点 | 规则 |
|---|---|
| 重复登记 | 定义完全一致 → 幂等；定义有差异 → `409 identifier_conflict` |
| 新增字段 | 仅允许追加在末尾；同名同型幂等，同名异型 → `field_type_conflict` |
| 删字段 | 仍被列级血缘引用 → `field_in_use_by_lineage` |
| 改字段类型 | 一律拒绝 `type_change_not_allowed`（改用新增字段） |
| 实体退役 | 幂等；挂活跃血缘 → `409 entity_has_active_lineage`；退役为终态，不可复活 |
| 退役后授权 | 一律拒绝 `entity_retired`；退役实体在可见性判定点统一排除，任何入口（发现/血缘/影响分析）都不再出现 |
| 退役后查询 | 有权用户直查 → `410 entity_retired`；**无权用户 → `403 permission_denied`**（先查权限再查退役，不泄露存在过）；均区别于 `404 not_found` |
| 血缘撤销 | 幂等；撤销前的历史快照结论逐位保留，新快照不再参与展开 |
| 撤销后重建 | 同一五元组重建**确定生效**，自新快照起再次参与展开；但重建会重放首次建边的端点校验：列已删 → `column_not_found`，实体已退役 → `entity_retired`，任何快照无悬空活跃血缘 |
| 血缘环路 | 结果中 `cycles_detected` 显式给出环路径，不截断 |
| 影响分析 | 响应含 `snapshot_id`；带该 id 重算结论逐位一致 |
| 越权访问 | `403 permission_denied`（区别于 `404 not_found`） |
| 分页 | token 内嵌快照号；翻页期间的新写入不影响本轮结果 |

详见 [`docs/design.md`](docs/design.md)。
