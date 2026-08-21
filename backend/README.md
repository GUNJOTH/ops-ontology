# FastAPI 后端

后端提供设备候选查询、上下文详情、规则智能体、预览、回放、审批、发布和审计接口。源库只读，流程状态写入 `system/data/semantic_workflow.sqlite3`。

## 标准启动

从项目根目录执行：

```powershell
# FastAPI 热更新 + Vite HMR
python backend\dev_start.py

# 只启动后端并启用热更新
python backend\dev_start.py --backend-only

# 修改后做一次启动/健康检查，检查结束自动退出
python backend\dev_start.py --check
```

兼容入口仍保留：

```powershell
.\backend\start_backend.ps1 -Reload
```

新开发、测试和排错统一使用 `backend/dev_start.py`。`backend/watch_backend.py` 只由标准启动器调用；不要同时手工启动多个 Uvicorn、watcher 或 `start_backend.ps1`。

默认地址：`http://127.0.0.1:8001`。

启动器规则：

1. 健康服务已存在时复用，不重复占用端口；
2. 端口已被非健康服务占用时失败退出，并提示处理占用者；
3. `--check` 使用一次性进程，不进入 watcher 循环；
4. 正常退出时只清理本次启动的子进程，不终止复用的已有服务。

## 依赖和配置

```powershell
Set-Location 'D:\项目\同海\ops-ontology\backend'
.\install_backend.ps1
```


`backend/.deps` 同时是 `system/` 脚本的统一 Python 依赖目录；不要恢复 `system/.deps` 或根目录 `.deps`。`system/requirements.txt` 仅作兼容提示，不要单独安装。

本地 AI 配置复制 [`backend/.env.example`](.env.example) 为 `.env` 后填写。常用变量为 `RULE_AGENT_BASE_URL`、`RULE_AGENT_MODEL`、`RULE_AGENT_API_KEY`、`RULE_AGENT_TIMEOUT`、`RULE_AGENT_MAX_TOKENS` 和 `RULE_AGENT_MAX_PROPOSALS`。`.env` 永远不要提交。

AI 未配置时，普通候选查询、预览、回放和审批仍可运行；只有规则发现接口会返回未配置状态。

## 主要接口

- `GET /api/health`
- `GET /api/metrics`
- `GET /api/dashboard`
- `GET /api/candidates`
- `GET /api/candidates/{candidate_id}`
- `GET /api/review-sample`
- `POST /api/reviews`
- `/api/rule-agent/*`：规则发现、草案和规则运行记录
- `/api/cleaning/*`：清洗任务、预览、回放、审批和发布
- `/api/semantic/canonical/*`：Canonical Semantic Model 的摘要、RDF 语句和设备 JSON-LD
- `GET /api/semantic/source-of-truth`：Canonical RDF 读取权威切换状态和覆盖率
- `POST /api/semantic/sparql`：只读 SPARQL 1.1 SELECT/ASK，禁止更新和外部 SERVICE
- `GET /api/semantic/releases`：Semantic Release 注册表和当前激活指针
- `/api/semantic/releases/{release_id}/*`：本地备份、审批、激活和回滚门禁；不写源系统

接口查询使用 SQLite 和 DuckDB；标准语义查询只读 Canonical Semantic Model。已投影对象的世界模型语义关系、事件、事实和当前状态从 Canonical RDF 读取；未投影对象会由 `/api/semantic/source-of-truth` 明确标记为兼容读取。审批、回放、发布和审计只写 SQLite 流程库，不回写 MaxiEAM/DM8。

## 代码变更验证

```powershell
python backend\dev_start.py --check
python -m compileall backend\app backend\dev_start.py backend\watch_backend.py
```

规则脚本是一次性命令，保存文件不会自动执行。只有后端 `app/` 代码和标准启动脚本参与热更新。

## 自动化测试

测试统一用 uv 管理环境，一键运行（首次会自动把「运行时 + 测试」依赖 vendor 到 `backend/.deps`，幂等可重复）：

```powershell
.\backend\run_tests.ps1
```

只跑纯单元测试：

```powershell
.\backend\run_tests.ps1 -m "unit"
```

跳过语义门禁（避免重跑标准语义 CI）：

```powershell
.\backend\run_tests.ps1 -m "not semantic"
```

`run_tests.ps1` 内部自动完成：定位 uv 与 uv 管理的 Python 3.12 → uv 缓存落到 `backend/.uv-cache`（可写、已 gitignore）→ `uv pip install --target .deps` 安装运行时+测试依赖 → 设置 `PYTHONPATH`/`PYTHONNOUSERSITE` 后运行 pytest。任意 pytest 参数原样透传。

`tests/` 按层组织：`unit/`（纯函数单测）、`data/`（事务/审计/幂等/迁移）、`api/`（端点与只读冒烟）、`integration/`（全 HTTP 栈集成测试）、`semantic/`（标准语义门禁包装）。

**集成测试（integration）口径**：用 `TestClient(app)` 走完整 HTTP 栈，但**不进入 `with` 上下文**（避免触发 startup 迁移写真实工作流库）；只读用例直读真实只读数据源，缺库/空数据用 `pytest.skip`；写路径用例把 `app.main` 的工作流库连接 monkeypatch 到临时库。一律断言 `sourceWrite=False`/`formalPublication=False`。测试写路径只落内存/临时库，不触碰 `system/data` 生产库。
