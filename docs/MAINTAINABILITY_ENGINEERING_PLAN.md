# 语义工程可维护性收口计划

本项目已有的设备语义治理、本体模型、Canonical RDF、SHACL、SPARQL、来源追溯和审批边界继续保留。本计划只解决工程化问题，不新增重复的业务对象表或第二套语义模型。

## 已落地的第一批基础能力

- `system/pipeline/`：共享步骤协议、内容哈希幂等键、只读 SQLite 连接、原子 manifest 写入和源写入/正式发布安全门禁。
- `system/pipelines/semantic_closure.json`：语义闭环 11 个步骤的声明式依赖图。
- `system/run_semantic_closure.py`：保留原命令入口，改为由 DAG runner 执行；支持 `--spec` 和 `--resume-manifest`，不改变源系统和正式发布边界。
- `system/tests/test_pipeline_framework.py`：覆盖 DAG 顺序、幂等键、来源 manifest、只读连接和安全边界；已纳入后端 pytest 的默认测试路径。
- `semantic_registry.validate_registry_contract()`：校验关系域/值域、别名目标和事件谓词注册；补齐 `related_device` 的正式关系映射。
- 第一批闭环构建器已使用共享 `connect_local` / `connect_readonly`：统一语义、事件、运行契约、治理契约、状态、事实、决策、执行台账、覆盖报告、Canonical 投影和 OWL RL 回放。
- 系统域的健康、指标和 Semantic Release 路由已迁入原生 `APIRouter`；其余业务域保持兼容入口，按低耦合顺序继续迁移。
- 工作流库已登记 `semantic_schema_migration`，生产迁移使用显式版本号，不再只依赖重复执行 `CREATE IF NOT EXISTS`。

## 运行约定

```powershell
$env:PYTHONPATH = 'D:\项目\同海\semantic-engineering\system;D:\项目\同海\semantic-engineering\backend\.deps;D:\项目\同海\semantic-engineering\backend'
python system\run_semantic_closure.py --help
python -m pytest backend\tests system\tests -q
```

执行实际闭环前仍须使用当前批准的本地快照，并保留生成的数据库备份。`--resume-manifest` 只复用同一 DAG、同一参数和同一幂等键的已完成步骤；不允许跳过安全门禁，也不允许写入源系统。

## 后续迁移顺序

1. 将 11 个闭环步骤的数据库连接、manifest 和安全断言继续收敛到共享 `pipeline` 库。
2. 按域把 `main.py` 路由迁移到原生 `APIRouter`，先迁低耦合的查询域，再迁审批和发布域。
3. 把现有手工 SQL 迁移纳入版本化迁移记录；迁移只作用于本地语义运行库。
4. 把 `semantic_registry` 和 `namespace.json` 接入标准 CI；任何未注册关系、未契约化 IRI 或不安全标志直接失败。
5. 为脚本补齐三类固定测试：正常执行、幂等重跑、脏数据隔离。按批次迁移，不一次性重写全部脚本。
6. 在数据规模和查询指标达到触发条件前，继续使用 SQLite + DuckDB + Parquet；如需外部三元组库，另行进行容量和迁移评估。

## 不在本轮做的事情

- 不重建大库，不重投影 Canonical RDF，不修改 DM8、MaxiEAM、HD_SAAS、XNY_SAAS 源表。
- 不自动合并不确定的跨系统身份、位置或业务关系。
- 不为了“框架化”立即引入 Airflow、GraphDB 或 Alembic；先用本地可审计的轻量协议验证收益。
