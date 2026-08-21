# 流程库与分析库

`system/` 是本地语义治理与本体运行层，不是源库镜像。Canonical RDF Dataset 是标准语义权威；关系表只承担映射、治理、回放、审批、审计和查询索引职责。系统继续提供 RDF/JSON-LD 投影、SHACL 校验、SPARQL 语义查询和 PROV-O 来源追溯，不要求立即引入外部图数据库。

标准要求与迁移阶段见 [`../docs/STANDARD_SEMANTIC_ENGINEERING_REQUIREMENTS.md`](../docs/STANDARD_SEMANTIC_ENGINEERING_REQUIREMENTS.md)。

## 两个数据库的职责

- `data/semantic_workflow.sqlite3`：唯一流程真相。保存源快照、批次、设备身份、候选、规则、预览、回放、审批、正式结果和审计。
- `data/semantic_analytics_v155.duckdb`：分析查询库。保存宽表和上下文，负责质量统计、站点分布、规则影响分析和导出。
- `data/unified_semantics.sqlite3`：本地语义覆盖层。保存对象身份、关系契约、事件投影、状态、事实、规则、行动和质量台账；不回写源系统。

## 双库基线

SQLite 与 DuckDB 必须使用同一个 `batch_id`、`source_snapshot_id` 和源快照哈希，不能只比较行数。当前正式本地基线为：

- 批次：`semantic-rule-approval-semantic-rule-approval-queue-20260816T100527Z`
- 源快照：`semantic-source-identity-layer-v1-20260816T063454Z-rule-queue`
- 候选、设备身份和 DuckDB fact：`397758`
- 当前批次验证明细：`397758 × 5 = 1988790`

从 SQLite 最新批次重建 DuckDB 使用 `build_duckdb_from_sqlite.py`；完整校验使用 `verify_system.py`。旧的 `382785` 是历史 HD-only 快照，不能再作为当前 SQLite 批次的验收分母。
- SQLite 的审批/发布状态优先；DuckDB 不作为审批状态权威副本。
- 两库通过 `candidate_id`、`batch_id` 和 `source_snapshot_id` 对齐。

## 统一生命周期

```text
cleaning task
  → preview
  → replay
  → approval
  → formal publication
```

每一步都必须能通过 manifest、规则版本、回放结果和审计事件追溯。源表永不回写。

## 声明式闭环与共享执行底座

`pipeline/` 是所有新管线步骤的共享边界，统一提供：

- 只读 SQLite 源连接（`mode=ro` + `query_only`）；
- 参数和依赖结果内容哈希形成的幂等键；
- 原子写入的步骤 manifest 和 provenance；
- `sourceWrite=False`、`formalPublication=False` 的结果安全门禁；
- 依赖、重试和受控恢复。

语义闭环的步骤依赖定义在 `pipelines/semantic_closure.json`，当前包含 11 个步骤。旧命令仍保留，但只作为入口适配器；新增步骤应先登记到 DAG，再实现 handler。可用 `--resume-manifest` 复用幂等键一致且已完成的步骤，不跳过回放、审批或正式发布门禁。

## 脚本命名约定

`system/` 下的一次性脚本按动作前缀分类：

- `init_*`：初始化本地数据库；
- `build_*`：构建 DuckDB 分析库；
- `generate_*`：只生成预览、样本或候选；
- `replay_*`：只执行回放，不改变正式结果；
- `confirm_*` / `promote_*`：写入审批决定或审批队列；
- `activate_*`：启用已经回放通过并确认的规则；
- `publish_*` / `acceptance_*`：执行正式结果发布和发布门禁；
- `verify_*` / `diagnose_*`：只读验证和诊断。

规则脚本不能在导入时执行批量写入；正式发布必须显式执行，并且必须带备份、回放凭据和审批凭据。

## 标准命令

使用项目运行时：

```powershell
python system\verify_system.py
python system\init_system.py
python system\verify_world_model_p0.py
python system\verify_state_replay.py
python system\build_semantic_fact_builders.py
python system\verify_fact_builders.py
python system\verify_decision_action_layer.py
python system\build_semantic_execution_layer.py
python system\verify_rule_agent_runtime.py
python system\diagnose_governance_backlog.py
python system\build_semantic_governance_contract.py
python system\build_canonical_semantic_model.py
python system\verify_canonical_semantic_model.py

# 一键运行本地语义闭环（自动备份；只写本地覆盖层）
python system\run_semantic_closure.py
python system\verify_semantic_closure.py

# 维护性和共享底座回归
.\backend\run_tests.ps1
```

初始化或发布前先备份数据库；生产数据目录和备份目录由本地运行环境维护，不提交到 Git。

## 安全门禁

以下任一条件不满足，都只能停留在预览/待复核层：

1. 源快照、设备身份或候选 ID 不唯一；
2. 描述改写没有规则版本、上下文证据或候选哈希；
3. 回放存在失败或异常未隔离；
4. 缺少人工审批凭据；
5. `source_write` 不为 0；
6. 发布前没有可定位的数据库备份。

## 当前数据库快照

状态数字以本地 SQLite 为准。当前批次共有 397,758 个候选，正式结果层 382,780 条；正式审批队列待审批 397,775 条。元数据语义字典为 76,286 条，9,391 条结构问题保持隔离；源写入与正式发布标记均为 0。需要刷新时运行 `verify_system.py` 和 `diagnose_governance_backlog.py`，不要手工修改本文件中的历史记录。

AI 智能体调用采用 OpenAI 兼容接口，默认最多 2 次有限尝试；403/401 等权限错误不重试，连接/超时/5xx 可有限重试，非法 JSON 只进入失败审计。模型调用失败不会使用本地目录结果冒充 AI；只有模型成功但没有绑定本地规则时，才会在审计中明确记录确定性目录兜底。

## 本体运行层回放与行动边界

- 状态回放按 `effective_at/occurred_at → recorded_at → source row` 排序；缺少有效时间、迁移规则或守卫证据的事件进入 `needs_review`。
- 可使用 `execute_state_transitions.py --subject-type device --subject-key <key>` 对单一主体重放；每次重放写入 `semantic_state_replay_diff`，用于比较当前状态变化。
- Fact Builder 只消费明确的数值、单位、事件时间、缺陷描述和严重程度字段，生成 Measurement、History、Trend、RepeatedDefect、SevereDefect、RiskAssessment 事实，并写入派生证据。
- `run_semantic_closure.py` 按事件投影 → 运行契约 → 治理契约 → 状态回放 → Fact Builder → 决策/行动 → Execution Ledger → 覆盖报告的顺序运行；缺失源证据登记为 `needs_evidence` 或 `isolated`，不自动补事实、不自动审批、不启用外部执行器。
- `/api/world-model/coverage` 和“本体元模型”页面显示最近闭环运行、覆盖缺口及审批/元数据积压，避免页面只显示空的业务计数。
- `build_semantic_runtime_contract.py` 建立来源权威、通用对象实例、带有效期的身份断言、事件 `correlation_id/previous_event_id` 和约束违规台账；这些是本体运行契约，不是源表复制。
- `/api/world-model/runtime-contract` 提供来源权威优先级、对象/身份/事件追踪计数和约束隔离结果。
- `/api/world-model/governance-contract` 提供身份自动/人工门禁、冲突隔离、强类型关系、对象属性、事件因果契约、可执行规则和质量缺口。
- `/api/semantic/canonical/summary`、`/api/semantic/canonical/device/{source_namespace}/{canonical_key}` 和 `/api/semantic/sparql` 只读访问 Canonical Semantic Model；前者读取标准投影台账，后两者不访问源表。
- `canonical_semantic.sqlite3` 只保存 RDF Dataset 的本地查询投影；标准交付物在 `system/canonical-runs/<run_id>/`，包括 TriG、JSON-LD 和 SHACL 验证报告。
- `semantic-governance-contract-v1` 的核心门禁是：唯一精确证据才能自动映射；源键复用到多个统一设备时隔离；缺少位置或跨事件证据时只注册契约、不自动推断；映射可通过 `lifecycle_status=revoked` 保留撤销审计。
- Execution Adapter 当前只有 `local-preview-adapter`，只登记本地执行台账，不调用外部系统；未来接入外部系统必须增加独立适配器、审批凭据和执行回执。
