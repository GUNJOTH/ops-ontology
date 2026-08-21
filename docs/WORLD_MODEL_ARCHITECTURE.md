# 企业运维世界模型实施基线

## 定位

平台围绕业务世界组织数据，而不是围绕源表组织页面：

`Object → Relation → Event → State → Fact → Rule → Decision → Action`

源 DM8、MaxiEAM、HD_SAAS、XNY_SAAS 始终只读。Canonical RDF Dataset 是标准语义权威；SQLite 中的 `semantic_*` 是身份映射、治理、回放、审批和执行台账运行层，DuckDB 继续承担大批量分析和回放。二者不能被误认为本体本身。

## Canonical Semantic Model

```text
源系统只读快照
        ↓
身份映射/证据/治理运行层（semantic_*）
        ↓ 只读投影
Canonical RDF Dataset
  RDF + RDFS + OWL 2 RL + SKOS
  SHACL 约束 + PROV-O 追溯
        ↓
SPARQL / JSON-LD / Agent Semantic Facade
```

标准本体文件位于 `standards/ontology.ttl`，SKOS 词表位于 `standards/vocabularies.ttl`，SHACL 约束位于 `standards/enterprise-operations.shacl.ttl`，JSON-LD context 位于 `standards/context.jsonld`。RDF Dataset 契约位于 `standards/rdf-dataset.json`，SPARQL 契约位于 `sparql/manifest.json` 及其查询文件。`build_canonical_semantic_model.py` 每次从本地语义运行层生成带命名图、版本和来源台账的 TriG/JSON-LD；它不修改源系统，也不把关系表伪装成 RDF。

关系和对象注册不再由多份脚本各自推断：`system/semantic_registry.py` 是关系键、RDF 谓词和对象 RDF 类的显式映射权威；`ontology_relation_type` 和 `ontology_object_type` 保存该映射的运行时快照，`business_object_relation` 仅作为兼容关系入口。

## 事实边界

- `SourceFact`：源系统原值和来源定位。
- `NormalizedFact`：经过已审批字典或确定性转换得到的标准值。
- `DerivedFact`：确定性规则计算结果。
- `Hypothesis`：AI 推测，不能自动升级为事实。
- `Decision`：由规则或人工形成的正式业务判断。
- `ActionPlan`：对外部世界的变更建议，必须单独审批。

## 当前实施状态

| 阶段 | 状态 | 当前实现 |
|---|---|---|
| P0 标准缺陷状态 | 已完成首版 | 8 个 Canonical Defect State、映射版本、审核流水、状态回放门禁已实现；14 条源状态候选仍待人工确认 |
| P1 统一事件投影 | 已完成首版 | 已将 14 条巡检、8 条缺陷、67 条工单事实投影为统一事件，并提供事件中心查询与详情证据链 |
| P2 状态迁移引擎 | 已完成首版 | 已将 8 条标准缺陷状态事实投影为 8 条迁移记录和 8 条当前状态快照；冲突且无时间证据时进入待复核 |
| P3 分层事实 | 进行中 | 来源事实、确定性派生事实和证据链已分表保存 |
| P4 规则体系 | 进行中 | 确定性规则登记、执行和回放已存在；规则发现与启用保持分离 |
| P5 决策与行动 | 已完成首版 | 105 条规则判断已进入决策台账；ActionPlan、审批凭据和源写入门禁已实现。当前没有明确行动规则，因此行动计划 0 条，不擅自生成工单 |
| P5.1 统一 Action 目录 | 已完成首版 | 已建立 `semantic_action_definition`：业务含义、前置条件、输入 Fact、权限范围、MCP/Workflow/API 映射、效果和执行状态机；外部适配器默认 disabled |
| M1 本体元模型 | 已完成首版 | 已建立对象、属性、关系、事件、缺陷状态机和迁移规则注册表；未知关系/事件只标记 `needs_review` |
| Canonical Semantic Model | 已完成受控测试投影 | 6 个命名图、5,211 个资源、30,769 条 RDF 语句；受控身份图覆盖 5,000 台设备；source/derived provenance 覆盖率 100%；13 个 SHACL NodeShape 通过；RDF/TriG/JSON-LD 可解析，标准门禁 0 个错误 |
| SKOS Vocabulary | 已接入版本化词表 | 153 条静态词表语句、7 个业务概念、8 个 Canonical 状态、14 个源状态候选、6 个规则术语；源状态映射仍按审批门禁执行 |
| OWL 2 RL Replay | 已接入可回放子集 | `owl2rl-replay-subset-v1` 在受控测试图中生成 38 条推理语句，2 轮收敛，结果进入独立 inference graph |
| Version / CI Gate | 已接入 | ontology 版本注册、快照、回滚指针和统一标准 CI 验证；未通过验证的版本不激活 |
| Agent Semantic Facade | 已完成首版 | 已提供设备 World Context、时间线、Fact Explain、Decision Explain 契约，页面入口为 `/ontology-runtime` |

## 状态映射闭环

1. 保留源状态原值和来源快照。
2. 人工选择 `NEW / PENDING / PROCESSING / RESOLVED / CLOSED / CANCELLED / SUSPENDED / UNKNOWN`。
3. 每次确认生成新的 `mapping_version` 和审核记录。
4. 回放只读取已确认映射，生成 `canonical_defect_state` 规范化事实。
5. 回放不生成外部行动，不修改源系统。

## 决策与行动门禁

1. `semantic_fact` 中的来源/派生事实作为输入，保留源表、源行和快照证据。
2. `semantic_rule_decision` 只登记确定性规则判断，并保存规则版本、输入事实、解释和置信度。
3. 只有显式的 `semantic_escalation_action` 才能物化为 `semantic_action_plan`；没有行动证据的判断不猜测成工单。
4. 每个 `semantic_action_plan` 可关联 `semantic_action_definition`，展示业务含义、前置条件、权限和效果；适配器映射只描述“如何做”，默认不启用。
5. 需要审批的行动生成 `semantic_action_approval`，在 `/decisions` 中单独批准或驳回。
6. 当前审批只改变本地行动计划状态并生成凭据，`source_write=0`、`formal_publication=0`，没有源系统执行器。

当前状态：已登记 `SAR:defect-pending-risk-treatment` v1：缺陷状态为 `PENDING` 且存在 `risk_assessment.risk_score >= 3` 时，生成 `create_work_order` 待审批建议。当前正式快照没有 `PENDING` 状态和风险事实，因此真实回放为 0 命中、0 行动计划、0 待审批；隔离副本已验证命中、生成计划和审批通过链路，未写源系统。

## 元模型和 Agent 契约

`system/build_ontology_meta_model.py` 是本地元模型注册表入口，采用 additive migration，不覆盖已有运行表：

- `ontology_object_type`：业务对象类型、层级、实例化能力和审核状态；
- `ontology_property_type`：属性类型、值类型、单位维度、来源映射和约束；
- `ontology_relation_type`：关系的 domain/range、逆关系、基数和时态属性；
- `ontology_event_type`：事件注册、主体类型和是否影响状态；
- `ontology_state_machine` / `ontology_transition_rule`：状态机和合法迁移；
- `ontology_meta_model_run`：每次构建的版本化校验结果。

Agent 不直接查询数据库，而通过以下稳定契约读取本体运行层：

- `GET /api/world-model/device/{unified_device_id}/context`：设备、身份、关系、事件、状态、事实、适用规则、判断和待审批行动；
- `GET /api/world-model/device/{unified_device_id}/timeline`：事件与状态迁移时间线；
- `GET /api/world-model/facts/{fact_id}/explain`：事实来源、输入事实、规则版本和派生链；
- `GET /api/world-model/decisions/{decision_id}/explain`：判断输入、规则、派生事实和行动计划；
- `GET /api/ontology/meta-summary`：元模型覆盖率与待复核注册缺口。
- `GET /api/semantic/canonical/summary`：Canonical RDF Dataset 版本、命名图、语句和 provenance 覆盖；
- `GET /api/semantic/canonical/device/{source_namespace}/{canonical_key}`：单设备 JSON-LD 视图和来源记录；
- `POST /api/semantic/sparql`：只读 SPARQL 1.1 SELECT/ASK，禁止更新和外部 SERVICE。

这些接口均返回 `schemaVersion`、`traceId`、`sourceWrite=false`、`formalPublication=false`。未知 predicate/event_type 不自动变成 accepted，必须先登记或进入复核。
