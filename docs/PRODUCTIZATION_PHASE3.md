# 通用企业语义智能平台 V1：第三阶段落地说明

第三阶段聚焦“企业知识管理与语义上下文运行”，不新增第二套对象表，也不让 Agent 直接写入知识正式层、源系统或执行行动。

## 已落地的 P0/P1 基础

- `platform-core` 扩展知识类：`KnowledgeAsset`、`DocumentKnowledge`、`CaseKnowledge`、`ExpertKnowledge`、`KnowledgeFragment`、`KnowledgeVersion`、`KnowledgeConflict`、`KnowledgeRelease`。
- 增加知识版本、适用对象/类/属性、关联事件/事实/规则/行动、冲突和发布关系的 OWL ObjectProperty。
- 增加知识包、来源、内容哈希、提取模型、生命周期和质量字段的 OWL DatatypeProperty。
- 增加知识生命周期和冲突类型 SKOS 词表；现有 `Standard`、`Rule`、`SOP`、`Risk` 继续作为兼容的知识子类，不重复建同义类。
- 增加 KnowledgeAsset、KnowledgeVersion、KnowledgeFragment、KnowledgeConflict 的 SHACL 形状。
- 现有 `knowledge_asset*` 关系层增加可加字段，保留历史状态和旧数据；新增知识类型在本地索引中使用 `knowledge_kind` 表达，避免破坏历史约束。
- 增加只读 Knowledge Runtime Gate：Published/enabled 知识必须有当前版本、来源快照、合法本体绑定，且不能存在未解决的中高危冲突或已过期状态。
- 增加 Semantic API V3 只读入口：
  - `GET /api/semantic/knowledge`
  - `GET /api/semantic/knowledge/{id}`
  - `GET /api/semantic/knowledge/{id}/evidence`
  - `GET /api/semantic/knowledge/{id}/versions`
  - `GET /api/semantic/object/{type}/{key}/knowledge`
  - `GET /api/semantic/context/object/{type}/{key}`
  - `POST /api/semantic/context/build`
- Context Builder 只组合 Canonical RDF 对象、关系、状态、事件、事实、已发布知识、规则索引、证据和审批门控行动引用；未发布、冲突和缺少来源的知识不会进入默认 Agent Context。
- `verify_release_gates.py` 增加 Knowledge Runtime Gate；所有接口返回 `sourceWrite=false`、`formalPublication=false`。
- 增加受控文档导入与解析：TXT/MD/CSV、DOCX、PDF（可选 `pypdf`），文档和片段只进入本地 Draft 层，并保留哈希、解析器、页码/章节和快照证据。
- 增加候选知识抽取：已配置模型时使用 OpenAI-compatible 网关；未配置或调用失败时使用确定性保守回退；两种路径都只产生 Candidate，不直接发布。
- 增加人工审核、冲突检测、知识回放和 Knowledge Release manifest；发布前必须通过当前版本、来源快照、开放冲突和 Canonical Class 绑定门禁。
- 增加案例知识候选入口 `POST /api/semantic/knowledge/cases`；案例仍需回放、人工审核和 Knowledge Release。
- 增加本地语义检索 `GET /api/semantic/knowledge/retrieve`，组合结构化、全文、图绑定和来源证据；首版不启用未经验证的向量索引。
- 增加 Agent Semantic Contract `POST /api/semantic/agent/validate`，强制声明、证据 ID 和置信度绑定当前上下文；默认禁止发布知识和执行行动。
- 增加身份复核分诊 `GET /api/world-model/identity-review/triage`，将非设备业务记录与低置信度设备断言分开统计；不自动合并跨系统身份。

## 仍需按低风险批次完成

1. 将实际待导入文档放入受控 `pilots/knowledge/input` 后执行真实导入；当前测试使用仓库内文档，未伪造外部文档数据；
2. 对已导入候选补齐 SHACL 逐版本验证和人工决定；未知 `appliesTo` 类必须保持复核，不得强行发布；
3. 生成知识版本回放及新旧标准影响报告，并将回放结果写入版本统计；
4. 按实际检索需求选择向量适配器；在此之前保持 Canonical RDF + SQLite 为权威，明确返回 `vector.not_enabled`；
5. 498 条身份待复核记录中，440 条已按业务键型分入非设备业务记录分诊，58 条仍为低置信度；需要补充 KKS/位置/关系表证据或人工决定，不能自动合并。

## 验收边界

本阶段的“完成”以知识身份、来源、版本、生命周期、冲突隔离和统一上下文接口可验证为准，不以文档上传数量或页面数量为准。Ontology Release 与 Knowledge Release 继续分开，源系统保持只读。
