# 项目规范

## 1. 目标和边界

本项目建设企业运维语义治理与本体运行层：从 MaxiEAM/DM8/HD_SAAS/XNY_SAAS 只读抽取快照，围绕设备、位置、组织、巡检、缺陷、工单、事实、规则和行动建立统一语义，经过规则、标准校验、回放和审批后写入本地正式结果层。

项目定位：**Canonical RDF-first、Ontology-native、关系运行层兼容**。`.ttl` 本体、`.shacl.ttl` 约束、RDF Dataset、SPARQL 查询、SKOS 词表和 OWL 2 RL 回放是正式工程资产；SQLite/DuckDB 与 `semantic_*` 只服务于映射、治理、审批、回放、审计和分析运行。

本项目不修改源库，不未经批准跨系统静默合并。Canonical RDF Dataset 是标准语义表达的权威；现有 SQLite/DuckDB 与 `semantic_*` 关系层只承担映射、治理、回放、审批、审计和分析运行职责，不再作为标准语义定义源。

标准开发要求见 [`STANDARD_SEMANTIC_ENGINEERING_REQUIREMENTS.md`](STANDARD_SEMANTIC_ENGINEERING_REQUIREMENTS.md)。

### 标准基线

- RDF 1.1、RDFS 1.1、OWL 2、SKOS、SHACL 1.0、SPARQL 1.1、PROV-O、JSON-LD 1.1 为生产兼容基线。
- RDF 1.2、SHACL 1.2、SPARQL 1.2 只作为后续兼容性观察项。
- OWL 2 RL 优先用于可回放、可解释的规则式推理。
- 不重复创建 `business_object_property`、`business_relation_type`、`business_event`；分别使用现有 `semantic_object_property`、`semantic_relation_contract`、`semantic_event`。
- SKOS 词表统一覆盖设备分类、缺陷状态、单位和运维术语；词表来自现有语义契约并带版本。
- OWL 2 RL 只启用可解释、可回放的规则子集；每次推理必须有规则集版本、输入投影版本和本地派生图。
- Canonical 投影的 source/derived 语句 provenance 覆盖率必须为 100%；无法追溯的语句不得进入标准语义图。
- Ontology 版本必须先快照、再迁移、再回放验证，激活通过本地版本指针完成，回滚只切换到已验证版本，不删除历史快照。
- 关系和对象词汇统一由 `system/semantic_registry.py` 显式登记：SQLite 使用稳定关系键和对象类型键，Canonical RDF 使用登记的 RDF local name/class；旧 camelCase、snake_case 只作为输入别名，进入校验前必须归一化，禁止隐式字符串拼接。
- 派生事实必须携带输入快照证据：单一输入快照沿用原快照 ID，多快照输入使用稳定的 `derived:<digest>` 快照引用，并通过 `DerivedFactShape` 门禁；不得以空快照进入 Canonical 图。
- `semantic_*` 本体运行层的 `formal_publication` 永远为 0；设备描述清洗工作流中 `batch_run.formal_publication` 表示本地清洗结果层发布，二者数据库职责不同，不得跨层解释或复用为源系统写入标志。

## 2. 目录职责

| 层 | 目录 | 规范 |
| --- | --- | --- |
| 接入与服务 | `backend/` | API、启动器、规则智能体适配，不保存密钥 |
| 页面 | `frontend/` | 只做查询、预览和审批交互，不复制清洗规则 |
| 契约 | `contracts/` | 设备身份、字段和输出契约 |
| 规则 | `rules/` | 版本化 YAML；确定性规则先于 AI |
| 流程 | `system/` | SQLite/DuckDB 初始化、预览、回放、审批、发布 |
| 试点 | `pilots/<SOURCE>/` | 单库运行脚本和 manifest 证据 |
| 文档 | `docs/` | 规范和运行手册 |

本地数据库、依赖、日志和发布备份不进入 Git：`system/data/`、`system/backups/`、`.deps/`、`.env`、`*.log`。

前端请求只允许访问 FastAPI；禁止使用 Mock 数据或在接口失败时回退到伪造统计。测试数据应放在独立测试夹具中，不得进入正式页面请求路径。

## 3. 数据和身份规范

- 设备身份：`source_schema + SITEID + ASSETNUM`；`ASSETID` 只作源库证据。
- 位置身份：`source_schema + SITEID + LOCATION`。
- 原始描述必须保留；规范化描述必须有规则版本和候选哈希。
- `LOCATIONS.DESCRIPTION`、`LOCHIERARCHY`、分类、`ASSETSPEC`、`ASSETFEATURE`、KKS 和父子关系只作为上下文证据。
- 描述相同不代表设备相同；不依据显示名称合并设备。
- 原始值、规范化值、来源表/字段、快照和证据必须可追溯。

## 4. 规则生命周期

```text
发现 → 草案 → 预览 → 样本回放 → 全量回放 → 人工确认 → 启用 → 正式发布
```

规则注册至少包含：

- `rule_key`、`rule_version`、用途和适用范围；
- 输入字段和上下文依赖；
- 改写前后示例；
- 排除条件和异常码；
- 预览行数、回放行数、通过/失败/隔离数；
- 审批人、审批时间、审批凭据和发布批次。

AI 规则智能体只能读取当前本地高质量、未发布数据并生成草案。AI 结果不得直接写源库、不得跳过回放、不得自动审批或自动正式发布。

## 5. 产物规范

每个规则处理批次使用独立目录：

```text
pilots/<SOURCE>/<run_name>/
  manifest.json
  sample_200.csv
  rewrite_preview.csv
  replay_manifest.json
  activation_manifest.json
  publication_manifest.json
```

不完整的批次可以只包含实际生成的文件，但不能用同名文件覆盖历史批次。发布前数据库备份目录使用：

```text
system/backups/<event>-pre-YYYYMMDDTHHMMSSZ/
```

## 6. 标准启动和验证

```powershell
# 根目录
python backend\dev_start.py --check
python system\verify_system.py
npm run build --prefix frontend

# 长时间开发
.\start_dev.ps1
```

启动器必须做到：复用健康服务、拒绝重复端口、`--check` 自动退出、只清理本次启动的进程。不要同时手工运行多个 watcher。

## 7. 发布门禁

发布前必须确认：

1. 输入快照和身份键稳定；
2. 预览与规则版本一致；
3. 回放无失败，异常已隔离；
4. 审批决定和审批凭据齐全；
5. 源写入标记为 0；
6. 数据库备份可定位；
7. 发布后数量、搜索、分页、详情、导出和审计均无回归。

任一门禁失败，批次停留在预览、待复核或阻断状态，不进入正式结果层。

### 7.1 Canonical RDF Semantic Release

Canonical RDF 使用独立的 Semantic Release 生命周期，不复用设备描述清洗的
`batch_run.formal_publication`：

```text
Canonical 投影 → 标准门禁 PASS → Release 草案 → 生产审批
→ RDF Dataset 备份校验 → 激活本地版本指针 → 运行监控
```

标准入口：

```powershell
python system\verify_release_gates.py --output system\reports\semantic-release-gates.json
python system\semantic_release.py prepare --verification system\reports\semantic-release-gates.json
python system\semantic_release.py approve <release_id> --reviewer <name> --receipt <receipt>
python system\semantic_release.py backup <release_id>
python system\semantic_release.py activate <release_id>
python system\semantic_release.py restore <release_id> <empty-staging-dir>
python system\semantic_release.py rollback <release_id>
```

Release 清单位于 `system/releases/`，注册表位于
`system/data/semantic_release_registry.json`，激活指针位于
`system/data/semantic_release_active.json`。运行层优先读取激活指针指定的
Canonical run；没有激活指针时仅作为开发兼容行为读取最新完成 run。

RDF Dataset 备份包含 Canonical SQLite、TriG、JSON-LD、推理结果和标准资产，
每个文件记录大小与 SHA-256。恢复默认只落入新的 staging 目录，不覆盖当前运行库。
必须完成校验和人工确认后才能切换运行指针；该机制不修改源系统。

### 7.2 线上监控与性能指标

- `GET /api/health`：SQLite、DuckDB、Canonical RDF、激活 Release 和运行状态；
- `GET /api/metrics`：Prometheus 兼容文本指标；
- 记录 HTTP 请求数、状态码和接口耗时，不记录设备描述、源键、凭据或 RDF 业务载荷；
- 设备语义详情读取必须观测延迟，异常时优先检查 Canonical SQLite 索引和当前 Release 指针；
- `sourceWrite=false`、`formalPublication=false` 必须作为健康和指标输出中的固定安全标签。

## 8. 变更方式

- 新增规则：优先增加规则注册/参数和通用执行器能力，不修改页面流程。
- 新增页面字段：先改 API 类型和响应契约，再改页面。
- 数据库结构：先更新 schema 和迁移/兼容逻辑，再运行只读验证。
- 启动逻辑：只维护 `backend/dev_start.py` 为标准入口；兼容脚本只做委托。
- 任何批量操作：先生成预览，再执行回放和审批；不要在开发服务器热更新回调中执行批量写入。
