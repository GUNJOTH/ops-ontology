# 贡献指南

提交变更前，请阅读 README.md、docs/PROJECT_STANDARD.md 和 docs/WORLD_MODEL_ARCHITECTURE.md，确认变更属于本仓库并理解源系统只读、Canonical RDF-first 和发布审批边界。

## 开发流程

1. 从最新 main 创建用途明确的开发分支。
2. 说明问题、影响范围和来源证据，保持小步提交。
3. 对标准资产、规则、验证脚本和文档保持同一可审查闭环。
4. Pull Request 必须记录实际验证命令、结果、未验证范围和发布门禁影响。
5. 等待 CI 与审查完成后再请求合并。

## 变更要求

- 设备身份遵循 source_schema、SITEID、ASSETNUM 等既有契约，不把描述文本当作身份键。
- 不向 MaxiEAM、DM8 或其他源系统写入数据。
- 不把 SQLite、DuckDB 或 semantic_* 关系表描述成正式本体定义源。
- 标准资产变更说明 OWL/RDF、SKOS、SHACL、SPARQL、PROV-O 或 JSON-LD 的影响。
- 规则、行动和审批变更说明回放、权限、审计、版本和回滚影响。
- 不提交真实业务数据、凭据、API key、数据库文件或未经审查的发布产物。

## 本地检查

至少执行与改动相关的检查：

- python -m compileall -q backend system
- python -m pytest backend/tests system/tests -q --tb=short
- npm ci --prefix frontend
- npm run build --prefix frontend

外部系统不可用时，应在 PR 中明确标为未验证或阻塞，不用模拟成功替代真实验收。