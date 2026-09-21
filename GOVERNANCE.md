# 项目治理约定

ops-ontology 是面向企业运维语义的本体运行平台。标准语义资产以 OWL/RDF、SKOS、SHACL、SPARQL、PROV-O 和 JSON-LD 为准；backend 与 system 负责映射、验证、回放、审批、审计和运行支撑。源系统保持只读，正式发布必须经过验证和审批门禁。

## 分支和审查

- 默认分支为 main，受保护的 main 只接受 Pull Request，不直接推送。
- 日常开发使用用途明确的短生命周期分支；未合并分支不因整理而删除，应通过 PR、提交说明或关联任务标明用途。
- 标准资产、映射、规则、关系契约和发布脚本的变更，应由熟悉影响边界的审查者复核。
- 审查重点是来源证据、身份键、只读边界、版本与回滚、验证结果和安全标志；没有证据的内容保持待复核，不默认通过。

## CI 与质量门禁

.github/workflows/ci.yml 在 main 的 Pull Request 和推送上执行：

- backend 与 system 的 Python 编译检查；
- backend/tests 与 system/tests 的受控测试；
- frontend 的锁定依赖安装和生产构建。

CI 通过不等同于生产全量验收。标准资产变更还应提供解析、约束、推理、查询、来源、版本和回滚证据；生产切换需要部署、性能和业务证据。

## 数据和发布边界

- 不提交生产数据库、源库凭据、API key、令牌或本地发布快照。
- 源库和源表只读；审批和发布只写本地覆盖层或受控发布目录。
- AI 只能发现规则、生成草案和附带证据，不能自动审批或直接正式发布。
- 正式发布应保留版本、备份、激活、回滚和审计记录。

## 治理入口

- 贡献流程：CONTRIBUTING.md
- 安全问题：SECURITY.md
- Issue 与 Pull Request：.github/ISSUE_TEMPLATE 和 .github/PULL_REQUEST_TEMPLATE.md
- 架构与运行边界：README.md、docs/PROJECT_STANDARD.md、docs/WORLD_MODEL_ARCHITECTURE.md