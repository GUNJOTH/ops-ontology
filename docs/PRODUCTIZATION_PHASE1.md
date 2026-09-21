# 通用企业语义智能平台 V1：产品化第一阶段

## 目标

本阶段把现有供热运维语义能力收口为可扩展的平台内核与行业包边界：

```text
Platform Core
      +
Thermal Operations Ontology Package
      +
Customer extensions
```

本阶段不新增供热业务对象，不复制 `semantic_*` 表，也不修改 DM8、MaxiEAM、HD_SAAS 或 XNY_SAAS 源表。

## 已落地的边界

### Platform Core

`packages/platform-core/` 归属与登记：

- `BusinessObject`、`Assertion`、`BusinessEvent`、`Fact`、`State`；
- `Knowledge`、`Rule`、`Decision`、`Action`；
- `IdentityAssertion`、`RelationAssertion`、`HumanReview`；
- 通用来源、版本、证据和结构化行动语义属性。

### Thermal Operations Package

`packages/thermal-operations/` 归属与登记：

- `Device`、`Site`、`Location`、`Inspection`、`Defect`、`WorkOrder`；
- 巡检、缺陷、消缺、工单和位置关系；
- 供热运维事件、状态机、规则和行动目录入口。

当前两个包使用原生 `semantic-package-v2` 模块。`packages/manifest.json` 通过
Resolver 解析依赖，Composition Engine 将包内 Turtle/SHACL/SKOS/JSON-LD
组合为 `builds/canonical/v2/`，再提升到 `standards/v2/` 作为版本化发布快照；
因此不会形成第二套运行时本体。

## 包资产契约

每个包必须包含并登记：

```text
ontology/<module>.ttl
shapes/<module>.shacl.ttl
vocabularies/<module>.ttl
context.jsonld
mappings/manifest.json
rules/manifest.json
events/manifest.json
state-machines/manifest.json
actions/manifest.json
tests/manifest.json
```

包注册表为 `packages/manifest.json`。系统会检查：

1. 包 ID、版本和依赖唯一且存在；
2. 所有包资产存在、可解析且在包目录内；
3. Core 与行业包的 Class、ObjectProperty、DatatypeProperty 不重复归属；
4. 当前 Canonical Ontology 的本体词汇全部有且只有一个包归属；
5. 包和资产的 `sourceWrite`、`formalPublication` 均为 `false`；
6. 依赖解析、命名空间、声明冲突和 Canonical promoted asset drift 均不得通过。

门禁入口：

```powershell
python system\verify_semantic_packages.py
python system\compose_ontology.py --output builds\canonical\v2
```

标准 CI 和 Ontology Release Gate 已自动调用该检查。包检查通过不等于生产发布；仍需通过 RDF、SHACL、OWL 2 RL、SPARQL、JSON-LD、Provenance、审批、备份和回滚门禁。

## Semantic API V1 / V2

Agent 和前端优先使用 Canonical RDF 只读接口：

```text
GET /api/semantic/packages
GET /api/semantic/packages/{package_id}
GET /api/semantic/ontology/catalog
GET /api/semantic/source-of-truth
GET /api/semantic/canonical/summary
GET /api/semantic/object/{object_type}/{canonical_key}
GET /api/semantic/object/{object_type}/{canonical_key}/relations
GET /api/semantic/object/{object_type}/{canonical_key}/events
GET /api/semantic/object/{object_type}/{canonical_key}/facts
GET /api/semantic/object/{object_type}/{canonical_key}/evidence
GET /api/semantic/object/{object_type}/{canonical_key}/timeline
POST /api/semantic/sparql
```

对象接口对多个匹配返回 `ambiguous=true` 和全部受控匹配，不跨系统静默合并。接口只读，响应固定包含 `sourceWrite=false` 和 `formalPublication=false`。

后续 `evaluate_rule`、`get_available_actions`、`create_action_plan` 继续复用现有规则/决策/行动审批层；创建行动计划不能绕过审批，也不能直接写源系统。

## 后续工作

1. 为每个行业包补齐真实映射、事件和 SHACL 样例；
2. 将 JSON 行动语义继续迁移为 RDF 结构；
3. 为 Semantic API 补充稳定 JSON Schema、权限和性能基线；
4. 通过完整回放后再切换 Semantic Source of Truth，不改变源系统。
