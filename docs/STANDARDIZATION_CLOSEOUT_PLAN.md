# 企业运维本体运行平台标准化收口计划

## 目标

本阶段不再扩展业务对象，不重复建设 `semantic_*` 业务表。目标是把已有语义治理成果收口为：

```text
关系运行层
    ↓ 映射
Canonical RDF Dataset
    ↓
OWL / SKOS / SHACL / PROV-O
    ↓
SPARQL / JSON-LD / OWL 2 RL 回放
    ↓
本体运行服务
```

源系统和源表继续只读；SQLite、DuckDB 和 `semantic_*` 只承担映射、治理、审批、回放、审计和分析运行职责。

## 既定标准基线

- RDF 1.1 / RDFS 1.1
- OWL 2 RL
- SKOS
- SHACL 1.0
- SPARQL 1.1
- PROV-O
- JSON-LD 1.1

暂不引入外部 GraphDB/Fuseki 作为前置条件。当前本地 RDF Dataset、TriG、JSON-LD、SQLite 投影和只读 SPARQL API 已能支撑开发与验收；外部 RDF Store 作为部署扩展，不改变标准语义模型。

## 四阶段路线

### 阶段 1：正式本体建模（第 1～4 周）

1. 固化 Namespace/IRI 契约；
2. 以 `standards/ontology.ttl` 作为当前唯一 OWL/RDFS 定义源；
3. 以 `standards/vocabularies.ttl` 作为 SKOS 词表源；
4. 以 `standards/enterprise-operations.shacl.ttl` 作为约束源；
5. 禁止通过新增业务表替代本体对象、属性和关系定义。

### 阶段 2：语义运行层（第 5～8 周）

1. 从关系运行层生成 Canonical RDF Dataset；
2. 按 ontology/source/derived/provenance/inference 划分命名图；
3. 执行 SHACL 验证并生成报告；
4. 执行 OWL 2 RL 有界回放；
5. 通过只读 SPARQL 和 JSON-LD 输出语义上下文。

### 阶段 3：业务迁移与融合（第 9～12 周）

1. 设备 360 查询优先读取 Canonical RDF 语义上下文；
2. 巡检、异常、缺陷、工单和消缺沿统一 Event/causedBy/generatedFrom 关系迁移；
3. 状态由事件和确定性 Transition Rule 派生；
4. 所有 Fact、Decision、ActionPlan 保留 PROV-O 来源和审批凭据；
5. 未确认身份和关系继续隔离，不自动合并。

### 阶段 4：标准验收与生产化（第 13～14 周）

1. SPARQL 查询契约、JSON-LD 契约和 SHACL 报告进入 CI；
2. 本体版本发布、迁移、快照、回滚形成闭环；
3. 验收 Canonical RDF 是否成为标准语义 Source of Truth；
4. 验收源系统只读、正式发布和外部行动审批门禁；
5. 输出标准化交付文档和差异报告。

## 当前实现盘点

| P0 | 当前状态 | 说明 |
|---|---|---|
| P0-01 Namespace/IRI | 已收口 | `standards/namespace.json` 为唯一命名空间契约，构建器和回放器已读取 |
| P0-02 OWL 本体 | 基本完成 | `ontology.ttl` 是当前唯一正式定义源，不重复拆出冲突文件 |
| P0-03 RDF Dataset | 已完成 | Canonical SQLite 投影 + TriG + JSON-LD，六类命名图；完整身份图覆盖 2,587,451 台设备 |
| P0-04 RDF Mapping | 基本完成 | 现有本地 Mapping Engine，源系统只读；R2RML 暂不作为前置条件 |
| P0-05 SHACL | 已完成基础门禁 | 12 类 NodeShape、验证报告和 CI 已接入；后续补标准实现级校验器 |
| P0-06 SPARQL | 已完成基础能力 | 只读查询契约、回放和 API 已接入 |
| P0-07 JSON-LD | 已完成 | 当前本体属性 75/75 已映射 |
| P0-08 PROV-O | 已完成基础闭环 | 当前 Canonical 可追溯语句 provenance 覆盖率 100% |
| P0-09 Ontology 版本 | 部分完成 | v1、迁移模板和回滚策略已有，需补发布/回滚验收 |
| P0-10 Source of Truth 切换 | 已完成首轮切换 | Canonical RDF 已覆盖当前身份快照中的 2,587,451 台设备，覆盖率 100%；读取适配器、覆盖率 API 和切换门禁均通过，未确认身份仍以隔离记录保留，不被自动合并 |

## 当前阻塞

- 4 条身份冲突、23 条待确认映射仍需保留隔离；
- Canonical RDF 全量身份投影已经完成；语义关系、事件、事实和状态仍严格按已有证据投影，缺少证据的关系继续隔离，不因设备已进入 RDF 就虚构上下文；
- 状态、事实和行动审批还需用真实业务证据完成一次端到端回放；
- 版本发布、迁移、回滚需要真实验收记录；
- SHACL 当前已有本地验证器和标准资产门禁，仍需补齐完整 SHACL Core 实现能力。

当前切换检查入口：`GET /api/semantic/source-of-truth` 或 `python system/verify_source_truth_cutover.py`。当前身份快照覆盖率为 100%，返回 `PASS`；关系、事件、事实和状态的证据覆盖仍由各自 provenance 和隔离状态约束。

## 验收原则

只有同时满足以下条件，才认为标准化收口完成：

1. 可通过 SPARQL 查询设备完整语义上下文；
2. 可沿事件、规则和来源解释当前状态；
3. 新导入数据可通过 SHACL 约束验证；
4. 任一结论可沿 PROV-O 追溯到源记录和治理活动；
5. 新增设备类别只需修改本体/词表资产和版本迁移，不需要新增业务表结构。
