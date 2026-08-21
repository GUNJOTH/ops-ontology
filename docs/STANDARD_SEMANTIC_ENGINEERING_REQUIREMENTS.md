# 企业运维本体语义运行平台：标准要求

版本：`standard-semantic-engineering-v2-20260821`

本文件是本公共项目的标准基线和工程要求。它约束语义模型、数据交换、校验、查询、推理、来源追溯、版本发布和审批边界，不要求一次性重建数据库，也不允许修改 HD/XNY/DM8/MaxiEAM 源系统。

## 1. 总体目标

建设可互操作、可验证、可回放和可治理的企业运维本体语义运行层：

```text
只读源快照
  -> 统一业务对象与身份证据
  -> RDF/RDFS/OWL 语义投影
  -> SHACL 约束校验
  -> SPARQL 语义查询
  -> PROV-O 来源追溯
  -> OWL 2 RL/确定性规则推导
  -> 预览/回放/审批/本地发布
```

标准化的目标不是把所有数据搬到图数据库，而是让同一个设备、位置、事件、规则和知识资产具有稳定的身份、关系、约束、来源、版本和可解释结果。

本体不是把所有数据搬到一起，而是让不同数据围绕同一个业务对象和统一语义被理解、关联、验证、推理和安全行动。

## 2. 标准基线

生产基线优先使用已经发布的 W3C Recommendation。RDF 1.2、SHACL 1.2 和 SPARQL 1.2 当前只作为兼容性观察项，不能成为本项目的必选运行依赖。

| 能力 | 生产基线 | 本项目用途 | 当前策略 |
| --- | --- | --- | --- |
| 图数据模型 | RDF 1.1 | 设备、位置、事件、关系和事实的标准表达 | 先从现有关系层只读投影 |
| 词汇/类型 | RDFS 1.1 | 类、子类、属性、domain、range | 与对象模型对应 |
| 本体约束语义 | OWL 2；优先 OWL 2 RL | 类型关系、等价/互斥和可计算推理 | 只启用可解释、可回放的子集 |
| 分类和术语 | SKOS | 设备分类、状态字典、单位、同义词、KKS 词表 | 维护概念、首选标签、别名和映射关系 |
| 数据形状校验 | SHACL 1.0 | 必填属性、类型、基数、合法关系和门禁 | 回放/审批前必须校验 |
| 语义查询 | SPARQL 1.1 | 设备全景、事件链、规则命中和来源查询 | 初期允许 SQL 翻译适配器 |
| 来源追溯 | PROV-O | 源快照、转换、规则、审批、发布和派生事实 | 与现有审计台账对齐 |
| API 交换 | JSON-LD 1.1 | 向前端、Agent 和外部服务输出带上下文的 JSON | 保持普通 JSON 兼容 |

ODRL 只在后续需要表达权限、禁止和义务时引入。SWRL、OWL-S 不作为当前生产标准基线；现有确定性规则和行动审批契约继续保留，并在后续通过 RDF/OWL/SHACL/PROV-O 进行标准化映射。

## 3. 现有模型到标准模型的映射

不重复创建基础表，现有模型是过渡运行层和标准适配层的唯一来源：

| 现有模型 | 标准语义角色 | 约束 |
| --- | --- | --- |
| `semantic_object_property` | 数据属性/对象属性定义；导出为 OWL property 和 SHACL property shape | 不再创建 `business_object_property` |
| `semantic_relation_contract` | 对象属性的 domain、range、基数和关系契约 | 不再创建 `business_relation_type` |
| `semantic_event` | `ex:BusinessEvent` 实例；按来源快照建立命名图 | 不再创建另一套 `business_event` |
| `semantic_identity_assertion` | 源身份到统一对象的带证据断言 | HD、XNY 默认不同命名空间；无证据不合并 |
| `semantic_identity_review` / audit | 身份断言的审批活动和决定 | 审批只写本地语义层 |
| `semantic_executable_rule` | 应用规则契约；可映射到 OWL RL 兼容推理或 SHACL 规则 | 不把应用规则冒充 SWRL |
| `semantic_state_replay*` | 状态投影、重放差异和时间证据 | 任何推导必须可回放 |
| `semantic_fact*` | 派生事实和证据 | 事实与源事实区分，保留生成活动 |
| `semantic_execution_ledger` | ActionPlan/执行台账的本地登记 | `source_write=0`，外部执行仍需独立审批 |
| SQLite / DuckDB | 关系型流程真相和分析副本 | 不作为标准语义格式；通过适配器投影/查询 |

## 4. 命名空间和身份要求

建议固定以下命名空间，并写入 JSON-LD context、RDF 导出和 SHACL 校验配置：

```text
ex:      https://semantic.local/ontology/
ex-hd:   https://semantic.local/source/hd/
ex-xny:  https://semantic.local/source/xny/
prov:    http://www.w3.org/ns/prov#
skos:    http://www.w3.org/2004/02/skos/core#
sh:      http://www.w3.org/ns/shacl#
owl:     http://www.w3.org/2002/07/owl#
rdfs:    http://www.w3.org/2000/01/rdf-schema#
xsd:     http://www.w3.org/2001/XMLSchema#
```

身份规则：

1. 统一设备 IRI 必须可由 `source_schema + SITEID + ASSETNUM` 或已审批的本地 `canonical_id` 稳定生成。
2. 源记录 IRI 必须带来源系统和源快照，不得用显示名称作为身份。
3. HD/XNY 先保持命名空间隔离；跨系统对应关系必须有证据、审批和有效期。
4. 事件 IRI 必须保留 `source_record_id`、`occurred_at`、`recorded_at`、`correlation_id` 和来源快照。
5. 规则、词表、形状、映射和本体都必须有版本、状态、生效时间和失效时间。
6. 同一条事实不能因不同导出格式产生两个身份；RDF、JSON-LD、SQL 视图必须引用同一个本地 canonical ID。

## 5. 必须实现的约束

### 对象和关系

- Device、Location、Organization、Inspection、Defect、WorkOrder、Rule、Fact、ActionPlan、Action、ActionExecution、ActionAdapter 必须有明确类型。
- 关系必须符合 `semantic_relation_contract` 的 source type、target type、cardinality 和生命周期。
- 设备位置关系必须保留有效期；位置迁移不能覆盖历史关系。
- `business_record_link` 只能作为兼容读取入口，新关系必须登记为强类型关系，不得继续扩展为万能关联表。

### 事件和状态

- 事件按 `effective_at/occurred_at -> recorded_at -> source row` 排序。
- 晚到事件、非法迁移、缺少主体或缺少时间证据必须进入 `needs_review`/`isolated`。
- `Inspection -> Defect -> WorkOrder -> Repair/Closure` 只有在真实源记录或已审批关系存在时才可物化。

### 规则和行动

- 规则必须登记输入事实、条件表达式、输出事实/决定、适用对象、版本、证据和审核状态。
- 大模型只能发现规则草案、分类证据或解释候选，不得替代确定性判断。
- `Fact -> Rule Decision -> ActionPlan -> Approval` 必须保留解释、证据、审批凭据和幂等键。
- Action 是独立业务资产，必须回答“业务含义、何时允许、需要哪些输入 Fact、谁有权、映射哪个适配器、成功后产生什么 New Fact”。
- ActionPlan 不是源系统写入授权；所有外部执行都必须经独立适配器、审批和回执。

## 6. 标准化开发阶段

### 阶段 0：标准基线和能力地图

交付：本文件、命名空间、IRI 规则、对象/关系/事件/规则到标准的映射表、版本策略。

验收：团队可以根据能力地图判断一个新增字段、关系或规则应该落在哪个现有模型和哪个标准对象上。

### 阶段 1：只读 RDF/JSON-LD 投影

新增标准适配器，不改源表：

1. 从 `semantic_*` 关系层导出 RDF 1.1 Turtle 和 JSON-LD 1.1。
2. 使用命名图区分 ontology、source snapshot、derived facts、approval 和 audit。
3. 导出清单必须包含批次、快照哈希、规则版本、行数、校验哈希和 `source_write=0`。
4. 对同一 canonical ID 做 Turtle/JSON-LD/SQL 交叉一致性检查。

### 阶段 2：SHACL 形状和质量门禁

为 Device、Location、IdentityAssertion、BusinessEvent、Defect、WorkOrder、Rule、Fact、ActionPlan、Action 建立 SHACL Core 形状，至少覆盖：

- 必填属性和数据类型；
- domain/range；
- 关系基数和重复身份；
- 时间顺序和有效期；
- 来源、快照、版本和审批凭据；
- HD/XNY 命名空间隔离；
- 不允许从缺失证据推导关系。

SHACL 失败只能进入隔离/复核，不得直接发布。

### 阶段 3：语义查询层

对外提供 SPARQL 1.1 语义查询契约，初期可以由 SQL adapter 翻译到 SQLite/DuckDB，但接口语义必须以 RDF 图模式定义。至少支持：

- 设备全景：身份、分类、位置、巡检、缺陷、工单、规则和来源；
- 时间范围内的设备事件链；
- 巡检异常对应的缺陷和工单来源；
- 规则命中的设备和证据；
- 当前状态及状态变化原因；
- 质量、隔离、审批和发布追溯。

查询结果同时提供 JSON-LD 和现有前端兼容 JSON，不能让前端直接拼源表 JOIN。

### 阶段 4：PROV-O 和版本快照

把源快照、清洗任务、投影、回放、规则判断、审批和本地发布表达为 `prov:Entity`、`prov:Activity`、`prov:Agent` 及其关系。每个正式结果必须回答：

```text
来自哪条源记录？
使用了哪个快照？
由哪个规则/推理活动产生？
谁在什么时候审批？
经过了哪些回放和校验？
当前是否仍有效？
```

### 阶段 5：可解释推理和回放

优先采用 OWL 2 RL 能够支持的规则式推理和现有确定性 Rule Runtime。每条派生事实必须带：输入事实、输入快照集合（单快照原值或稳定 `derived:<digest>` 引用）、规则版本、推理时间、结果、解释和 provenance。推理结果必须可以清空后重建，不能依赖不可追溯的模型记忆。

### 阶段 6：审批、发布和一致性验收

分别区分：

- 身份映射审批；
- 数据清洗审批；
- 本体/Schema 结构审批；
- 规则启用审批；
- ActionPlan 执行审批。

统一门禁仍为：

```text
发现/映射草案
  -> 标准投影
  -> SHACL 校验
  -> 回放
  -> 预览和样本
  -> 对应类型审批
  -> 本地发布
  -> PROV-O 追溯和导出验收
```

## 7. 不允许的实现方式

- 不修改 HD/XNY/DM8/MaxiEAM 源表。
- 不为同一语义重复创建 `business_object_property`、`business_relation_type`、`business_event` 等基础表。
- 不把 SQL 行数、页面统计或 Mock 数据当成 RDF/OWL/SHACL 一致性证据。
- 不把缺失位置、缺失因果关系、低置信身份或跨系统同名设备自动合并。
- 不以模型输出直接生成审批、启用、发布或外部执行动作。
- 不删除原始值、失败记录、隔离记录、撤销记录和历史快照；清理只能改变本地生命周期状态并保留审计。
- 不把 SQLite 和 DuckDB 都当成流程权威；SQLite 管流程和语义覆盖层，DuckDB 管分析副本。

## 8. 验收标准

阶段性标准验收至少包括：

1. RDF 1.1 Turtle 能解析，JSON-LD 1.1 能展开/压缩，IRI 无冲突。
2. SHACL Core 形状能对正例通过、对缺失身份/来源/非法关系/错误基数反例失败。
3. 同一设备全景可由标准查询契约返回，并能追溯到本地 canonical ID。
4. 同一派生事实可以回放重建，结果和 provenance 一致。
5. 审批拒绝、撤销、隔离和回滚都有本地审计，源写入始终为 0。
6. HD/XNY 分域查询不会隐式合并；跨域关系必须显式呈现证据和审批状态。
7. 全量回放后生成质量、差异、覆盖、审批积压和元数据积压报告。
8. 交付包只包含代码、标准文档、测试夹具、脱敏示例和运行说明，不包含密钥、源库凭据或生产快照。

## 9. 当前项目状态和下一步

当前 Canonical RDF 语义运行层已经具备统一对象、身份断言、关系契约、标准资产、OWL 2 RL 回放、SHACL 门禁、SPARQL 查询、JSON-LD 交换、来源追溯、Semantic Release、版本指针、备份恢复、回滚和监控能力；SQLite/DuckDB 与 `semantic_*` 只承担映射、治理、回放、审批和审计运行职责，不再作为标准语义定义源。

标准路线的受控 5,000 条只读测试基线已经通过项目标准门禁：HD_SAAS 与 XNY_SAAS 各 2,500 条设备，生成 6 个命名图、5,211 个资源和 30,769 条 RDF 语句；Canonical 身份覆盖 5,000 条设备，source/derived provenance 覆盖率 100%；OWL 2 RL 可回放子集生成 38 条推理语句并在 2 轮收敛；13 个 SHACL NodeShape 通过，JSON-LD context 属性覆盖 84/84，SPARQL 3 个查询契约通过；标准 Release、备份恢复、激活和跨版本回滚验证通过。测试基线只写入本地结果和报告，不写源系统、不进入真实生产发布。

当前仍保留的受控业务缺口和后续工作：

1. 5000 条受控测试基线仍有 498 条身份断言待复核，11 组业务事件证据处于 `needs_evidence`；
2. 当前测试基线没有足够的已确认巡检、缺陷和工单设备桥接，因此事实、事件和当前状态不自动生成；
3. 跨系统候选默认关闭，候选数为 0 不代表两个系统没有关系，只代表没有未经证据和审批的跨系统合并；
4. 继续用真实源记录补充设备—位置历史和巡检→缺陷→工单→消缺因果链；
5. 后续 ontology v2 变更必须继续经过迁移、回放、审批、备份、回滚和 CI 标准门禁；
6. 持续通过 `.github/workflows/semantic-ci.yml` 执行标准一致性测试和交付检查。

本次实现只写入本地 Canonical 结果、推理结果、版本注册和验证报告，不修改源表，不正式发布源系统数据。

## 10. 规范参考

- RDF 1.1 Concepts and Abstract Syntax: https://www.w3.org/TR/rdf11-concepts/
- RDF Schema 1.1: https://www.w3.org/TR/rdf-schema/
- OWL 2 Web Ontology Language Profiles: https://www.w3.org/TR/owl2-profiles/
- SHACL: https://www.w3.org/TR/shacl/
- SPARQL 1.1 Query Language: https://www.w3.org/TR/sparql11-query/
- SKOS Reference: https://www.w3.org/TR/skos-reference/
- PROV-O: https://www.w3.org/TR/prov-o/
- JSON-LD 1.1: https://www.w3.org/TR/json-ld11/
