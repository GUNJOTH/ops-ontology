# 企业运维本体世界平台

本项目以设备描述治理 Harness 为数据治理基础，并在本地关系型语义覆盖层上建设企业运维世界模型。平台围绕对象、关系、事件、状态、事实、规则、决策和行动组织 HD/XNY/MaxiEAM 的只读数据。

设备描述清洗与本体运行层是两个有边界的子系统：前者继续遵循“预览 → 回放 → 审批 → 发布”，后者通过统一业务对象和证据链提供机器可判断、可计算、可约束的业务语义。架构基线见 [`docs/WORLD_MODEL_ARCHITECTURE.md`](docs/WORLD_MODEL_ARCHITECTURE.md)。

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

## 当前本地库快照

截至 2026-08-19，当前 SQLite/DuckDB 同步基线为：

- 当前批次：`semantic-rule-approval-semantic-rule-approval-queue-20260816T100527Z`；
- 输入/候选/设备身份/DuckDB fact：397,758 条；
- 正式语义结果层：382,780 条；
- 正式审批队列待审批：397,775 条，其中 397,758 条建议保留原文、17 条为改写建议；
- 清洗任务：162 条待审批、1 条草稿、3 条已发布；
- 源写入与正式发布标记：均为 0（正式结果层是本地覆盖层）；
- 元数据语义字典：76,286 条，另有 9,391 条结构发现问题保持隔离。

这些数字是本机数据库快照，不是源库实时统计。运行 `python system\verify_system.py` 和 `python system\diagnose_governance_backlog.py` 获取当前校验与积压分层结果。

## 变更原则

规则变化只改规则注册、规则参数或后端规则执行层；页面只负责展示和审批，不复制规则判断。任何新增规则都必须经过：草案、预览、样本回放、全量回放、人工确认、启用和审计。

闭环编排使用 `system/pipelines/semantic_closure.json` 作为步骤依赖的单一事实源；`run_semantic_closure.py` 只是兼容入口。后续脚本迁移应复用 `system/pipeline/`，不再复制 SQLite 只读连接、manifest、幂等键和安全标志样板。

本体运行层页面入口：`/semantic-events`、`/semantic-facts`、`/ontology-runtime`、`/decisions`。其中 `/ontology-runtime` 展示元模型注册表和待复核缺口，`/decisions` 展示规则判断、行动计划和审批凭据；当前行动批准不会调用源系统。
