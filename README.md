# 企业运维本体语义运行平台

这是一个面向公共使用的、标准驱动的企业运维本体语义运行平台。项目以统一业务对象和 Canonical RDF Dataset 为核心，将设备、位置、组织、巡检、缺陷、工单、事实、规则、状态、决策和行动组织成可理解、可验证、可追溯、可推理的语义模型。

本项目的核心理念是：本体不是把所有数据搬到一起，而是让不同数据围绕同一个业务对象和统一语义被理解、关联、验证、推理和安全行动。源系统保持只读，语义层通过身份、关系、事件、事实、规则和来源证据建立可解释的业务上下文。设备描述清洗是可独立运行的治理子系统，本体运行层不依赖 Mock 数据，也不把清洗流程误认为本体定义。

项目定位：**Ontology-driven、Canonical RDF-first、standard-gated、关系运行层兼容**。正式标准资产包括 OWL/RDF 本体、SKOS 词表、SHACL 形状、RDF Dataset、SPARQL 查询、PROV-O 来源和 JSON-LD 交换契约；SQLite/DuckDB 与 `semantic_*` 只承担映射、治理、回放、审批、审计和分析运行职责。

架构基线见 [`docs/WORLD_MODEL_ARCHITECTURE.md`](docs/WORLD_MODEL_ARCHITECTURE.md)，工程规范见 [`docs/PROJECT_STANDARD.md`](docs/PROJECT_STANDARD.md)。

## 标准执行与遵守

标准不是文档口号，而是项目的强制工程门禁：

```text
标准要求
  → OWL/RDF/SKOS/SHACL/SPARQL/PROV-O/JSON-LD 资产
  → Canonical 投影
  → RDF/OWL RL/SHACL/SPARQL/JSON-LD 验证
  → Release 审批、备份、激活和回滚
  → 运行健康检查与 Prometheus 指标
```

- 生产兼容基线为 RDF 1.1、RDFS 1.1、OWL 2 RL、SKOS、SHACL 1.0、SPARQL 1.1、PROV-O 和 JSON-LD 1.1。
- `.ttl`、`.shacl.ttl`、RDF Dataset、SPARQL manifest、SKOS vocabulary、JSON-LD context 和版本清单是标准工程资产。
- 任何标准资产、映射、规则或关系契约变更，都必须经过解析、约束、推理、查询、来源、版本和回滚门禁。
- “项目标准门禁通过”表示本项目验证链通过，不等同于第三方 W3C 认证；生产切换还需要部署、性能和业务证据验收。

## 先看这里

```powershell
# 在项目根目录执行：启动 FastAPI 热更新 + Vite HMR
.\start_dev.ps1

# 修改代码后做一次无残留启动自检
python backend\dev_start.py --check

# 后端依赖和前端构建检查
python system\verify_system.py
npm run build --prefix frontend

# 构建本地决策 → 行动计划 → 审批台账（不写源系统）
python system\build_decision_action_layer.py

# 构建统一业务 Action 目录（业务含义/前置条件/权限/适配器映射/效果）
python system\build_semantic_action_catalog.py
python system\verify_semantic_action_catalog.py


# 构建对象/属性/关系/事件/状态机元模型注册表（只写本地语义覆盖层）
python system\build_ontology_meta_model.py

# 本体运行层只读验收（未知注册缺口会显示 PASS_WITH_REVIEW）
python system\verify_ontology_runtime.py
python system\verify_source_truth_cutover.py

# Canonical RDF Semantic Release 控制面
python system\semantic_release.py --help

# 声明式语义闭环与维护性回归
python system\run_semantic_closure.py --help
.\backend\run_tests.ps1

# P0 状态映射验证、行动审批隔离回放、AI 运行门禁和积压诊断
python system\verify_world_model_p0.py
python system\verify_state_replay.py
python system\build_semantic_fact_builders.py
python system\verify_fact_builders.py
python system\verify_decision_action_layer.py
python system\build_semantic_execution_layer.py
python system\verify_rule_agent_runtime.py
python system\diagnose_governance_backlog.py
```

默认地址：

- 前端：<http://127.0.0.1:5173>
- FastAPI：<http://127.0.0.1:8001>
- 健康检查：<http://127.0.0.1:8001/api/health>
- 运行指标：<http://127.0.0.1:8001/api/metrics>

`backend/dev_start.py` 是唯一标准启动入口。它会复用健康的已有服务；端口已被非本项目服务占用时直接报错，不重复启动并进入重启循环。

## 工作流

```text
只读快照 → 质量范围 → 规则发现/注册 → 预览 → 回放 → 审批 → 启用 → 正式发布
```

- 源库和源表只读，任何规则都不能直接修改 MaxiEAM 或 DM8。
- 确定性规则优先；AI 只发现规则并生成草案，不直接写正式结果。
- 预览、回放、审批凭据和审计记录都保存在本地流程库。
- 只有回放通过且带审批凭据的结果才允许进入正式结果层。
- 设备身份使用 `source_schema + SITEID + ASSETNUM`；描述不是身份键。
- 位置、分类、规格、KKS 和父子关系用于上下文校验，不擅自拼接或改写设备名称。
- 本体运行层以 Canonical RDF Dataset 为标准语义权威；SQLite/DuckDB 与 `semantic_*` 仅承担映射、治理、回放、审批、审计和分析运行职责，不把关系表当作本体定义源。
- 决策运行层使用 `Fact → Rule Decision → ActionPlan → Approval`；没有显式行动证据时不生成工单，审批只写本地覆盖层。

## 依赖

- Python：统一使用 `backend/requirements.lock` 安装到 `backend/.deps`，backend 与 system 共用；`system/requirements.txt` 已废弃，不要单独安装。
- 前端：使用 `frontend/package-lock.json`，安装到 `frontend/node_modules`。
- 快速安装/测试：`.\backend\install_backend.ps1`、`.\backend\run_tests.ps1`。
- 详细规则见 `AGENTS.md` 与 `docs/PROJECT_STANDARD.md`。

## 项目分层

| 目录 | 职责 | 是否提交源码 |
| --- | --- | --- |
| `backend/` | FastAPI、规则智能体接口、启动与热更新 | 是 |
| `frontend/` | React + TypeScript + Vite + Ant Design 页面 | 是 |
| `contracts/` | 统一设备语义和身份契约 | 是 |
| `rules/` | 确定性规则、术语和质量口径 | 是 |
| `system/` | SQLite/DuckDB 初始化、验证、回放、审批、发布脚本 | 是 |
| `system/pipeline/` | 统一步骤协议、幂等键、只读边界、provenance manifest 和 DAG runner | 是 |
| `pilots/HD_SAAS/` | HD 单库试点的脚本、manifest 和可追溯证据 | 按产物规范提交 |
| `docs/` | 项目规范、运行手册和发布门禁 | 是 |
| `system/data/` | 本地 SQLite/DuckDB 数据库 | 否 |
| `system/backups/` | 正式发布前数据库备份 | 否，保留在本机 |

详细约定见 [`docs/PROJECT_STANDARD.md`](docs/PROJECT_STANDARD.md)。

## AI 规则智能体

本地配置文件为 `backend/.env`，已被 Git 忽略；只提交 [`backend/.env.example`](backend/.env.example)，不要把 API key 写入代码、日志或 manifest。前端不提供 Mock 回退，后端不可用时页面必须明确报错，不能显示伪造统计。

智能体的权限边界是：读取当前本地高质量未发布数据，输出规则草案和证据；不能源库写入、不能跳过回放、不能自动审批、不能直接正式发布。

## 受控测试基线

仓库不提交生产数据库、源库凭据或本地发布快照。当前已验证的受控测试基线为两个只读域各 2,500 条设备，共 5,000 条：

- Canonical RDF：6 个命名图、5,211 个资源、30,769 条语句；
- 设备身份图覆盖 5,000 条设备，来源/派生语句 provenance 覆盖率 100%；
- OWL 2 RL 回放生成 38 条可解释推理语句；
- SHACL 13 个形状通过，JSON-LD context 属性覆盖 84/84，SPARQL 3 个查询契约通过；
- 标准 Release、备份恢复、激活和跨版本回滚验证通过；
- `sourceWrite=false`、`formalPublication=false`，未修改源系统。

这只是标准运行和身份投影测试基线，不代表生产全量业务闭环。无明确设备身份桥接证据的业务记录必须保持待复核或隔离，不得被统计为已确认事实。

## 变更原则

规则变化只改规则注册、规则参数或后端规则执行层；页面只负责展示和审批，不复制规则判断。任何新增规则都必须经过：草案、预览、样本回放、全量回放、人工确认、启用和审计。

闭环编排使用 `system/pipelines/semantic_closure.json` 作为步骤依赖的单一事实源；`run_semantic_closure.py` 只是兼容入口。后续脚本迁移应复用 `system/pipeline/`，不再复制 SQLite 只读连接、manifest、幂等键和安全标志样板。

本体运行层页面入口：`/semantic-events`、`/semantic-facts`、`/ontology-runtime`、`/decisions`。其中 `/ontology-runtime` 展示元模型注册表和待复核缺口，`/decisions` 展示规则判断、行动计划和审批凭据；当前行动批准不会调用源系统。
