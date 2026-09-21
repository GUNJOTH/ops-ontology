# 语义工程可维护性收口计划

本项目已有的设备语义治理、本体模型、Canonical RDF、SHACL、SPARQL、来源追溯和审批边界继续保留。本计划只解决工程化问题，不新增重复的业务对象表或第二套语义模型。

## 已落地能力

- `system/pipeline/`：共享步骤协议、内容哈希幂等键、只读 SQLite 连接、原子 manifest 写入和源写入/正式发布安全门禁。
- `system/pipeline/entrypoint.py`：提供 `run_single_step()` 单步执行封装，统一 `PipelineContext`、manifest、断点续跑和正式发布能力门禁。
- `system/pipelines/semantic_closure.json`：语义闭环 11 个步骤的声明式依赖图。
- `system/run_semantic_closure.py`：保留原命令入口，改为由 DAG runner 执行；支持 `--spec` 和 `--resume-manifest`，不改变源系统和正式发布边界。
- `system/tests/test_pipeline_framework.py`：覆盖 DAG 顺序、幂等键、来源 manifest、只读连接、单步入口和安全边界；已纳入后端 pytest 的默认测试路径。
- `semantic_registry.validate_registry_contract()`：校验关系域/值域、别名目标和事件谓词注册；补齐 `related_device` 的正式关系映射。
- 闭环构建器已使用共享 `connect_local` / `connect_readonly`：统一语义、事件、运行契约、治理契约、状态、事实、决策、执行台账、覆盖报告、Canonical 投影和 OWL RL 回放。
- 后端应用装配已收口：`backend/app/main.py` 为 12 行兼容入口，应用壳在 `app/bootstrap.py`，旧导入由 `app/compat.py` 保留；`backend/app/domains` 已无 `from app.main import` 反向依赖。
- 候选域已拆分：`candidates/service.py` 收敛为 95 行门面，拆出 `samples.py`、`cleaning_sync.py`、`ai_review.py`、`candidate_queries.py`、`approvals.py`。
- 规则智能体域已拆分：`rule_agent/service.py` 收敛为 91 行门面，拆出 `agent_gateway.py`、`canonicalization.py`、`catalog.py`、`discovery.py`、`lifecycle.py`、`proposals.py`、`queries.py`、`reasoning.py`、`replay.py`。
- system 脚本连接已收口：全部 `verify_*.py`、`replay_*.py` 不再直接调用 `sqlite3.connect`；8 个 `build_*.py` 和 5 个 `publish_*.py` 已迁移到共享连接工厂。
- 当前维护性预算 PASS：顶层 system 脚本 97 个，pipeline 使用 64 个，直接 `sqlite3.connect` 20 个；后端测试文件 32 个，system 测试文件 4 个；全量 **164 passed / 4 skipped**。
- 前端 `App.tsx` 已采用路由级懒加载；开发环境可用 `npm run check`，CI/正常终端用 `npm run build` 验证完整生产包。

## 运行约定

```powershell
$env:PYTHONPATH = 'D:\项目\同海\ops-ontology\system;D:\项目\同海\ops-ontology\backend\.deps;D:\项目\同海\ops-ontology\backend'
python system\run_semantic_closure.py --help
python -m pytest backend\tests system\tests -q
python system\verify_maintainability.py --strict
```

`verify_maintainability.py` 只扫描代码、测试和入口使用情况，不打开 SQLite/DuckDB，也不修改任何运行数据。预算定义在
`contracts/maintainability_budget.json`，用于阻止大文件、散装脚本和直接数据库连接数量继续增长；预算会随着迁移进展以独立提交下调。

执行实际闭环前仍须使用当前批准的本地快照，并保留生成的数据库备份。`--resume-manifest` 只复用同一 DAG、同一参数和同一幂等键的已完成步骤；不允许跳过安全门禁，也不允许写入源系统。

## 后续迁移顺序

1. 将 11 个闭环步骤的数据库连接、manifest 和安全断言继续收敛到共享 `pipeline` 库；verify/replay 已全部收口，下一批优先处理剩余 20 个直接 `sqlite3.connect` 的运维/回填/激活类脚本。
2. 继续清理 `app.compat` 兼容导出，待旧调用方全部迁移后删除桥接层；各域已使用原生 `APIRouter` 并由 `app.bootstrap` 挂载。
3. 把现有手工 SQL 迁移纳入版本化迁移记录；迁移只作用于本地语义运行库。
4. 把 `semantic_registry` 和 `namespace.json` 接入标准 CI；任何未注册关系、未契约化 IRI 或不安全标志直接失败。
5. 为脚本补齐三类固定测试：正常执行、幂等重跑、脏数据隔离。按批次迁移，不一次性重写全部脚本。
6. 在数据规模和查询指标达到触发条件前，继续使用 SQLite + DuckDB + Parquet；如需外部三元组库，另行进行容量和迁移评估。

## 本批退出条件

- 纯逻辑拆分后，旧 API 路径、路由数量和审批边界不变；
- 共享连接迁移脚本通过 Ruff、维护性预算和全量测试；
- 前端类型检查和正式生产构建均通过；
- 没有修改语义数据库、正式结果层或任何源系统表。

## 长期维护原则

- 新业务步骤必须先成为可测试的函数或 pipeline handler，再提供 CLI 入口；不得继续复制一个新的顶层脚本作为唯一实现。
- 任务应保持幂等、可重试、可恢复，并通过 manifest 记录输入快照、版本、输出和失败原因。
- API 采用域内 `APIRouter → service → core` 依赖方向；`main.py` 只负责应用生命周期、中间件和路由挂载。
- 后端、system 和前端都必须有 CI 门禁；构建失败、预算超限或测试失败不得进入发布分支。
- 所有维护性改造都不得改变源系统只读边界、Canonical RDF 来源追溯和审批发布门禁。

## 不在本轮做的事情

- 不重建大库，不重投影 Canonical RDF，不修改 DM8、MaxiEAM、HD_SAAS、XNY_SAAS 源表。
- 不自动合并不确定的跨系统身份、位置或业务关系。
- 不为了“框架化”立即引入 Airflow、GraphDB 或 Alembic；先用本地可审计的轻量协议验证收益。
