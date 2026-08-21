# 系统状态快照

更新时间：2026-08-20

> 本文件是人工可读快照；权威状态来自 `data/semantic_workflow.sqlite3` 和 `python system\verify_system.py`。

## 当前批次

- 批次：`semantic-rule-approval-semantic-rule-approval-queue-20260816T100527Z`
- 输入/候选/设备身份/DuckDB fact：397,758 条
- 正式结果层：382,780 条
- 正式审批队列待审批：397,775 条（其中 397,758 条建议保留原文，17 条为改写建议）
- 清洗任务：162 条待审批、1 条草稿、3 条已发布
- 源写入：0
- 正式发布：0（正式结果层为本地覆盖层）
- 批次状态：`completed`

## 当前运行约束

- 源 MaxiEAM/DM8 表只读；
- SQLite 是流程状态唯一真相；
- DuckDB 使用 v1.5.5，仅用于分析；
- AI 只生成规则草案，不直接审批或发布；
- 新规则必须经过预览、回放和人工确认；
- 正式发布前必须备份数据库。
- 行动层已完成正式库空匹配回放，并通过临时隔离夹具验证 `Fact → Decision → ActionPlan → Approval`、审批凭据和幂等性；当前正式快照没有达到风险阈值的 PENDING 事实，因此正式行动计划仍为 0。
- P0 状态映射验证脚本已通过；AI 运行失败已补齐 `error_code/retryable/attempt_count/fallback_used` 诊断字段。
- 状态回放已升级为 `state-replay-v2`：按 `effective_at/occurred_at → recorded_at → source row` 排序，支持主体级重放、晚到事件标记和状态差异报告；隔离夹具回放通过。
- Fact Builder 已注册并通过隔离夹具验证：Measurement、HistoryStatistic、Trend、RepeatedDefect、SevereDefect、RiskAssessment；正式快照本次没有明确测量或重复缺陷证据，因此生成 0 条风险事实。
- Execution Ledger 与 `local-preview-adapter` 已初始化；当前适配器只登记本地执行计划，不调用外部系统，source_write/formal_publication 均为 0。
- 已使用当前模型配置完成一次真实 50 条小批量语义推理回归，返回 3 个语义簇判断，未产生审批、启用或发布动作。
- 已运行统一闭环入口 `run_semantic_closure.py`：事件投影 89 条、状态回放 8/8 通过、事实构建完成、决策层完成、执行台账就绪；运行状态为 `completed_with_gaps`，备份已写入 `system/backups`。
- 已新增 `semantic_coverage_run` / `semantic_coverage_gap`，将缺失事件证据、风险证据、审批积压和元数据积压全部登记为 `needs_evidence` 或 `isolated`，不再以未追踪的“0 条”掩盖覆盖缺口。
- 已建立 Canonical RDF Semantic Model：`standards/ontology.ttl`、`standards/vocabularies.ttl`、`standards/enterprise-operations.shacl.ttl`、`standards/context.jsonld`、`standards/rdf-dataset.json`、`standards/semantic-asset-manifest.json` 和 `build_canonical_semantic_model.py`；SPARQL 契约位于 `sparql/`。最新只读投影为 6 个命名图、2,589,079 个资源、15,534,573 条 RDF 语句，其中完整身份图包含 2,587,451 台设备；source/derived provenance 覆盖率 100%，TriG/JSON-LD 可解析，12 个 SHACL NodeShape 覆盖设备、位置、身份断言、事件、缺陷、工单、事实（含源/派生）、规则、行动计划和位置历史关系，SHACL Core 门禁 0 个错误。全量身份图采用流式投影和独立文件校验，避免把数百万设备全部加载进内存。
- 关系/对象词表已收口到 `system/semantic_registry.py`：当前运行时 18 个关系类型均有稳定关系键和明确 RDF 谓词，27 个对象类型均有 RDF 类映射；`verify_ontology_runtime.py` 已增加关系/对象词表 reconcile 门禁，避免旧 snake_case/camelCase 注册表再次分裂。
- 已接入 `replay_owl_rl.py`：OWL 2 RL 可回放子集版本 `owl2rl-replay-subset-v1` 生成 1,067 条推理语句，2 轮收敛；推理结果单独保存于本地 inference graph，不写源系统。
- 已接入 `ontology_version_manager.py` 和 `.github/workflows/semantic-ci.yml`：版本注册、快照、回滚指针及标准资产/Canonical/SHACL/SPARQL/JSON-LD/OWL-RL 统一验证均可执行。
- Canonical 投影已纳入 `run_semantic_closure.py`；`verify_semantic_closure.py` 会检查标准投影、解析、校验错误和安全写入标记。后端已提供 `/api/semantic/canonical/summary`、设备 JSON-LD 和只读 `/api/semantic/sparql`。
- 已新增 `semantic_layer_authority`、`semantic_object_instance`、`semantic_identity_assertion`、`semantic_constraint_violation` 和 `semantic_runtime_contract_run`；当前 5 层权威策略、175 个对象实例、89 条身份断言、89/89 事件追踪，运行契约本身 0 条违规。
- 已新增 `semantic-governance-contract-v1`：66 条唯一精确身份映射可进入自动候选，14 条置信度/证据不足的映射需确认，4 组源键到多个统一设备的冲突已隔离；89 条业务记录已分成设备—巡检 14、设备—缺陷 8、设备—工单 67，未虚构缺陷—工单或巡检—缺陷关系。
- 已注册 17 条对象属性、10 条可执行版本化规则、6 类强类型业务关系和 4 类事件因果关系。当前 86 个设备位置关系、89 条事件因果链缺少明确证据，均保持隔离，不自动补写。

## 已知历史问题

早期文档曾记录 382,785 条 HD-only 历史快照；当前 SQLite/DuckDB 已统一到 397,758 条批次。以后以数据库查询和验证脚本结果为准，不以旧文档数字覆盖当前状态。

## 当前受控待补证据项

- 审批积压仍需按 `replay_id/site` 分层抽查后由人工批量审批；系统不会因“建议批准”自动写入审批凭据。
- 元数据字典已加载 76,286 条；9,391 条结构问题（属性父级缺失、对象表未匹配、索引定义缺失）继续隔离，需按 finding_type 确认或补证据。
- AI 智能体已有 14 次完成、8 次历史失败；当前有效模型配置的 50 条真实小批量回归已通过，历史失败仍保留为审计记录。
- HD/XNY 实时源库刷新尚未建设，当前仍以只读本地快照为准。
- 当前覆盖运行登记 11 个受控缺口：8 个缺失事件类型证据、1 个风险证据缺口、1 个审批积压、1 个元数据积压；缺口均已进入可追踪的 `needs_evidence/isolated` 状态。
- 最新闭环运行登记 12 个受控缺口：在上述积压外新增 1 个本体治理隔离项，来源为位置证据/因果证据缺失；这不是运行失败，而是防止系统把没有证据的关系当成事实。
- 外部行动执行仍未启用；当前只有 `local-preview-adapter`，执行台账可回放，但不会调用工单或源系统接口。
