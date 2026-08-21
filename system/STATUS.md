# 系统状态快照

更新时间：2026-08-21

> 本文件是人工可读快照；运行时权威状态来自本地验证报告、Canonical RDF Release 指针和健康接口。本文件不代表外部源库实时统计。

## 当前受控测试基线

- 测试快照：`identity-test-snapshot-5000-20260821T030346Z`；
- 数据域：HD_SAAS 与 XNY_SAAS，各 2,500 条设备，共 5,000 条；
- 业务及设备上下文记录：1,579 条；
- 源系统与源表：只读；`sourceWrite=false`；
- 本地正式源系统发布：未执行；`formalPublication=false`；
- 快照、结果和 Release 产物均带 manifest 与 SHA-256 校验。

## 身份与证据状态

- `unified_device`：5,000 条；
- 身份映射候选：1,579 条；
- 已接受：48 条；
- 待复核：197 条；
- 已隔离：1,334 条；
- 空身份：0 条；源系统内重复身份：0 条；
- 跨系统候选生成默认关闭，不根据缺少证据的名称、位置或描述自动合并；
- 业务事件、事实和当前状态只接受有明确设备身份桥接证据的记录。

## Canonical RDF 与标准验证

最新 Canonical 投影：`canonical-projection-20260821T033542Z-05dae53ab1`

- 命名图：6 个；
- 资源：5,211 个；
- RDF 语句：30,769 条；
- Canonical 身份设备：5,000 条；
- source/derived provenance 覆盖率：100%；
- OWL 2 RL：38 条推理语句，2 轮收敛；
- SHACL：13 个 NodeShape 通过；
- JSON-LD context：84/84 属性覆盖；
- SPARQL：3 个查询契约通过；
- RDF、TriG、JSON-LD、OWL RL、SHACL、SPARQL、Source Truth 门禁：PASS。

Canonical RDF 是标准语义权威。SQLite/DuckDB 与 `semantic_*` 只承担映射、治理、回放、审批、审计和分析运行职责，不承担本体定义职责。

## Release、版本与监控

- 本体版本：`enterprise-operations-ontology/v1`；
- 当前激活 Release：`semantic-release-20260821T033604Z-137f30a561`；
- Release 已完成审批、备份、恢复、激活、跨版本回滚和再次激活；
- `GET /api/health`：SQLite、DuckDB、Canonical RDF 和激活 Release 均正常；
- `GET /api/metrics`：Prometheus 指标可用；
- 监控报告：`system/reports/semantic-monitoring-5000.json`，状态 PASS。

## 受控业务缺口

- 498 条身份断言仍待复核；
- 11 组事件证据处于 `needs_evidence`；
- 当前测试基线没有足够的已确认巡检、缺陷和工单设备桥接，因此不自动生成事实、事件和当前状态；
- 缺少位置历史、因果链或跨系统身份证据的记录继续隔离；
- 外部行动适配器未启用，ActionPlan 不能直接写入工单系统或其他源系统。

## 强制安全边界

- 不修改 HD/XNY/DM8/MaxiEAM 源表；
- 不把 Mock 数据当作验证证据；
- 不把低置信身份或跨系统同名设备自动合并；
- 不以模型输出直接审批、启用、发布或执行行动；
- 所有标准资产、规则、映射和推理结果必须可回放、可追溯、可审批和可回滚。
