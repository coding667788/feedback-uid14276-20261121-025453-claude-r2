# 敏感配置变更的离线补偿账本

纯 Python 3.12 标准库实现，无第三方依赖、CPU 运行、SQLite 持久化。针对敏感配置事务，
支持在固定逻辑时刻规划并原子提交“完整事务回退”补偿序列，同时维护跨键服务不变式、
单键单调版本、审核人邻接规则与一次性审核消费。

## 1. 运行环境与命令

- Windows 原生 Python 3.12（开发验证版本 3.12.8），仅使用标准库（`sqlite3`、`json`、
  `hashlib`、`argparse`、`unittest`、`threading` 等）。
- 统一命令形式：

```
python -m solution <命令> CASE.json [--db DB.sqlite]
```

| 命令 | 需要 `--db` | 作用 |
|---|---|---|
| `validate` | 否 | 结构/语义校验、历史重放、不变式检查点、版本连续性检查 |
| `bootstrap` | 是 | 用案例初始化持久账本（幂等，已初始化则报 `E_DB_STATE`） |
| `plan` | 是 | 在案例 `logical_time` 固定逻辑时刻求可执行完整回退前缀 |
| `apply` | 是 | 按计划绑定的键版本与审核 ID 原子提交完整序列 |
| `submit-application` | 是 | 幂等提交补偿申请，回执稳定 |
| `register-approval` | 是 | 幂等注册审核决定 |
| `revoke-approval` | 是 | 撤销未消费审核（已消费审核不可撤销） |
| `inspect` | 是 | 只读查看活动/已撤销事务、当前版本、有效状态 |

标准输出为 UTF-8 JSON（键排序、缩进 2）。成功退出码 0；结构或业务错误退出码 2，
输出形如 `{"status":"error","code":"...","message":"...","at":"/字段/路径","details":{...}}`。

## 2. 输入案例（CASE.json）完整定义

顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string | 案例标识，非空 |
| `logical_time` | int ≥ 0 | 规划/申请执行的固定逻辑时刻 |
| `policy_generation` | int > 0 | 政策代次；审核与计划必须匹配当前代次 |
| `keys` | array | 每个配置键的有限值域与初值 |
| `constraints` | array | 必须始终成立的跨键不变式（可空数组） |
| `transactions` | array | 原事务历史，按 `committed_at` 唯一升序 |
| `applications` | array | 补偿申请（bootstrap 内置；也可后续提交） |
| `approvals` | array | 审核决定（可空） |
| `plan_request` | object | `plan` 命令入参，形如 `{"app_id": "..."}` |
| `apply` | object | `apply` 命令入参，内含 `plan` 对象 |
| `application` / `approval` / `approval_id` | object/string | 三个写命令的入参 |
| `fault` | object | 可选故障注入，`{"phase": "before_step_writes"|"before_commit"}` |

### 2.1 `keys[]`

```json
{"key": "a", "domain": ["a0", "a1"], "initial_value": "a0"}
```

- `domain` 非空，`initial_value` 必须落在值域内。
- 值可以是任意可 JSON 序列化标量（字符串、整数等），比较按 JSON 相等。

### 2.2 `constraints[]`（服务不变式）

```json
{
  "constraint_id": "g1",
  "active_key": "a",
  "active_values": ["a1", "a2"],
  "requires": [
    {"key": "b", "allowed": ["b1"]},
    {"key": "c", "allowed": ["c1"]}
  ]
}
```

语义：当 `active_key` 的当前值属于 `active_values` 时，同组每个 `requires[].key`
的当前值必须属于其 `allowed`。约束在以下检查点全部校验，绝不只在外层事务末校验，
也绝不发布中间态：

1. 启动状态；
2. 历史每个提交事件、每个历史回退事件之后；
3. 规划与 apply 中每一步完整补偿之后；
4. 最终状态。

某事务的**依赖键** = 它写入的键 ∪ 与它写入键处于同一约束组的只读键。
计划与 apply 会绑定并核对全部依赖键版本（含不变式只读键）。

### 2.3 `transactions[]`

```json
{
  "txn_id": "T1",
  "committed_at": 10,
  "category": "network",
  "weight": 1,
  "writes": [
    {"key": "a", "old_value": "a0", "new_value": "a1", "new_version": 1}
  ],
  "rolled_back": {"at": 50}
}
```

- 一个原事务可同时写多个键，但同一事务内键不可重复。
- 每个写保存旧值 `old_value`、新值 `new_value` 与该键的新版本 `new_version`。
- 单键版本从 0 起，每发生一次写事件（提交或补偿回退）严格 +1；`new_version`
  必须等于该键当前版本 +1，否则报 `E_VERSION`。
- `old_value` 必须等于历史重放到该点的实际值，否则报 `E_HISTORY`。
- `weight` 非负，用于闭包外附加补偿的代价计算；缺省 1。
- `rolled_back.at`（可选）标记该事务在历史上已被完整回退，时刻必须晚于 `committed_at`；
  历史回退同样恢复旧值并使相关键版本 +1，且经过不变式检查点。

### 2.4 `applications[]`

```json
{
  "app_id": "APP1",
  "target_txn_id": "T1",
  "idempotency_key": "idem-app1-0001",
  "window_start": 50,
  "window_end": 200,
  "requested_at": 60
}
```

补偿申请引用一个原事务和允许执行的时间窗口；`requested_at` 必须落在窗内。
窗内有效、审核仅可消费一次。目标事务若已撤销则报 `E_TXN_UNDONE`。

### 2.5 `approvals[]`

```json
{
  "approval_id": "AP-T1-alice",
  "app_id": "APP1",
  "txn_id": "T1",
  "reviewer": "alice",
  "bound_versions": {"a": 3, "b": 2},
  "decided_at": 70,
  "policy_generation": 1,
  "revoked": false
}
```

审核人按“回退事务 + 该步骤目标键当前版本”签发：

- `bound_versions` 的键集合必须恰好等于该事务的依赖键集合（`E_APPROVAL_SCOPE`）。
- `decided_at` 必须落在申请窗内；`policy_generation` 必须等于当前代次。
- 仅当审核未撤销/未消费、窗内、代次匹配、且每个绑定版本等于执行那一刻的当前版本时合格。
- 同一审核人可批准互不重叠（无共同键）的不同事务。
- 对任一键，按补偿序列筛出涉及该键的步骤，相邻两步的审核人必须不同（邻接规则）。
- 同一步存在多份合格审核时可任选一份；规划器按审核 ID 字典序决胜。

## 3. 核心语义

### 3.1 完整回退前缀与闭包

只允许撤回**完整原事务**。若目标事务的某个键当前由更晚的仍有效事务写入，
必须先完整补偿那个更晚事务。闭包 = 目标事务沿“写键链”传递可达的全部仍有效
更晚写事务（只按写键，不变式只读键不进入闭包）。

撤销某事务后：

- 该键的有效写入来源回到上一个尚未撤销的事务（或初值）；
- 单键版本继续 +1，不回退。

### 3.2 规划目标（按优先级字典序）

1. **最小化闭包外附加补偿代价**：允许为消除跨键不变式而额外补偿闭包外、已获准的事务，
   附加代价按被补偿事务的输入 `weight` 求和；
2. 最小化总补偿步数；
3. 按 `(事务 ID, 审核 ID)` 序列字典序决胜。

规划器在固定逻辑时刻运行，逐步按前序模拟结果核对版本；搜索采用 DFS + 分支备忘录
（等价状态 = 仍有效事务集合 + 每键上一审核人 + 闭包剩余集合）。闭包不可满足时，
输出最早缺失的审核或前置事务（含 `blocker_txn_ids` / `target_blocker_txn_ids`）。

`brute_force_plan` 是独立的穷举枚举器（仅限小案例），测试用它与 DFS 对拍。

### 3.3 apply 原子提交

`apply` 在单个 `BEGIN IMMEDIATE` 事务内：

1. 取写锁并重拍数据库权威快照（含每键当前版本，覆盖纯历史重放值）；
2. 按计划指纹做幂等查重，同计划重放返回同一回执；
3. 在当前逻辑时刻重跑规划器，要求 `(txn_id, approval_id)` 序列与每步
   `dep_versions` 完全一致；
4. 逐步核对：事务仍活动、前缀满足、审核合格（窗、代次、版本、撤销/消费状态）、
   审核人邻接规则、全部依赖键（含不变式只读键）版本与计划绑定一致；
5. 每步执行后立即校验不变式（不发布中间态），并保存逐步逻辑回执；
6. 任一步失败（过期、撤销、写入竞争、不变式破坏、政策代次漂移）整笔回滚，不提交。

## 4. 输出定义

### 4.1 `plan` 成功输出

```json
{
  "status": "ok",
  "app_id": "APP1",
  "target_txn_id": "T1",
  "logical_time": 100,
  "policy_generation": 1,
  "closure": ["T1", "T2"],
  "outside_cost": 7,
  "step_count": 3,
  "plan_id": "apply_<16hex>",
  "tiebreak": [["O", "AP-O"], ["T2", "AP-T2"], ["T1", "AP-T1"]],
  "steps": [
    {
      "seq": 1,
      "txn_id": "O",
      "approval_id": "AP-O",
      "reviewer": "rvO",
      "in_closure": false,
      "weight": 7,
      "dep_versions": {"w": 1, "y": 2}
    }
  ]
}
```

不可满足时 `status` 为 `unsatisfiable`，并带 `reason`（错误码、最早失败事务、
阻塞链等）。

### 4.2 `apply` 成功输出

```json
{
  "status": "applied",
  "receipt_id": "apply_<16hex>",
  "plan_id": "apply_<16hex>",
  "app_id": "APP1",
  "steps": [ /* 同 plan.steps，含逐步回执信息 */ ],
  "new_versions": {"a": 6, "b": 4},
  "final_state": {"a": "a0", "b": "b0"}
}
```

`receipt_id` 与 `plan_id` 同为计划指纹，因此同逻辑输入的重放天然得到同一稳定回执。

### 4.3 错误码（节选）

| 代码 | 含义 |
|---|---|
| `E_MISSING_FIELD` / `E_TYPE` / `E_VALUE` | 结构错误，`at` 指向字段路径 |
| `E_DOMAIN` | 值超出声明值域 |
| `E_VERSION` | 单键版本未严格 +1 |
| `E_HISTORY` | 历史旧值/回退值与重放不符 |
| `E_INVARIANT` | 不变式被违反（含检查点阶段） |
| `E_DUPLICATE` / `E_REF` | 重复标识 / 未知引用 |
| `E_APPROVAL_SCOPE` | 审核绑定键集合不等于依赖键 |
| `E_NO_APPROVAL` / `E_ADJACENCY` | 无合格审核 / 邻接审核人冲突 |
| `E_PREFIX` | 更晚写事务仍活动，前缀不满足 |
| `E_PLAN_STALE` / `E_WRITE_CONFLICT` | 计划过期 / 键版本写入竞争 |
| `E_POLICY_GENERATION` / `E_WINDOW_CLOSED` | 政策代次漂移 / 时窗失效 |
| `E_APPROVAL_INVALID` / `E_APPROVAL_CONSUMED` | 审核不可用 / 已消费 |
| `E_PLAN_TAMPERED` | plan_id 与计划指纹不符 |
| `E_PERSISTENCE` | 注入或真实持久化故障（整笔回滚） |
| `E_SQLITE` / `E_DB_STATE` / `E_SCHEMA` | 数据库层错误 |

## 5. 复现命令

```bash
# 1) 校验
python -m solution validate samples/sample01_diamond_plan.json

# 2) 初始化账本
python -m solution bootstrap samples/sample01_diamond_plan.json --db ledger.sqlite

# 3) 规划（跨键菱形依赖，含两份合格审核的决胜）
python -m solution plan samples/sample01_diamond_plan.json --db ledger.sqlite

# 4) 原子 apply，并再次执行验证幂等稳定回执
python -m solution apply samples/sample02_diamond_apply.json --db ledger2.sqlite
python -m solution apply samples/sample02_diamond_apply.json --db ledger2.sqlite

# 5) 持久化故障：整笔失败、无中间提交
python -m solution bootstrap samples/sample03_fault.json --db ledger3.sqlite
python -m solution apply samples/sample03_fault.json --db ledger3.sqlite

# 6) 幂等申请 / 审核注册 / 撤销
python -m solution submit-application samples/sample04_commands.json --db ledger4.sqlite
python -m solution register-approval samples/sample04_commands.json --db ledger4.sqlite
python -m solution revoke-approval   samples/sample04_commands.json --db ledger4.sqlite

# 7) 只读检查
python -m solution inspect samples/sample02_diamond_apply.json --db ledger2.sqlite

# 8) 断言测试（题目指定入口）
python -m unittest discover -s tests -v
```

## 6. 测试覆盖

`tests/` 下 50+ 个断言，覆盖题目要求的全部验证点：

- 跨键菱形依赖、闭包传递、完整回退前缀；
- 逐键版本严格递增、回退后有效来源回到上一未撤销事务；
- 补偿后同值但旧审核因版本漂移失效；
- 规划后无关键变更成功、有关键变更（含不变式只读键被并发修改）失败；
- 关联键并发 apply：恰有一笔成功、另一笔失败且无部分提交；
- 持久化故障（提交前/写入前两个注入点）整笔回滚，故障后重试成功且回执稳定；
- 同一步两份合格审核的决胜与“仅消费一份”；
- 两条不同合法顺序配不同审核后缀、按 `(txn_id, approval_id)` 决胜；
- 最终状态合法但中间违反不变式 → 拒绝且不发布中间态；
- 只有回退闭包外事务才有解，闭包外代价按 `weight` 优先；
- 相同审批 ID 的重放、回执后的进程退出（关闭重开 Store 回执一致）；
- 24 事务 / 80 键规模下规划性能与等价状态备忘录；
- DFS 规划器与独立穷举器在 60 个随机小案例上对拍；
- 结构化错误码与字段定位、JSON/文件错误、政策代次与 plan_id 篡改。

## 7. 目录结构

```
solution/
  __init__.py
  __main__.py        # python -m solution 入口
  cli.py             # 命令分发、JSON 输入输出
  errors.py          # LedgerError 与故障异常
  model.py           # 领域数据类
  validation.py      # 案例校验与历史重放（含不变式检查点）
  engine.py          # World 语义、闭包、资格判定、不变式
  planner.py         # DFS 规划器、备忘录、穷举对拍器、不可满足诊断
  apply.py           # 锁内重算、逐步核对、原子提交编排
  storage.py         # SQLite schema、幂等回执、故障注入
  util.py            # 规范化 JSON 与稳定摘要
tests/               # unittest 断言（helpers/builders 为虚构夹具构造器）
samples/             # 可直接运行的本地虚构样例
```
