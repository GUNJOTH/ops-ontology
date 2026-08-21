# 语义工程可维护性审计报告

> 基线日期：2026-08-21；本文只记录与当前项目状态一致的内容，历史快照不再保留。
> 方法：对 `backend/app`、`system/`、测试、依赖、CI、Git 历史做静态体检，并与长期维护型项目的通用工程基准对比。所有数据均为本仓库实测。

---

## 1. 当前状态快照

| 指标 | 当前值 |
|---|---:|
| `backend/app/main.py` | 12 行兼容入口；应用壳在 `app/bootstrap.py`，兼容导出在 `app/compat.py` |
| 最大后端模块 | `backend/app/domains/candidates/ai_review.py`，1112 行 |
| 最大 system 脚本 | `system/build_canonical_semantic_model.py`，1320 行 |
| system 顶层脚本 | 97 个 |
| 使用 pipeline 的顶层脚本 | 64 个 |
| 仍直接调用 `sqlite3.connect` 的顶层脚本 | 20 个 |
| 直接调用 `duckdb.connect` 的顶层脚本 | 7 个 |
| 后端测试文件 | 32 个 |
| system 测试文件 | 4 个 |
| 全量测试 | **164 passed / 4 skipped** |
| 静态检查 | Ruff 通过 |
| 维护性预算 | `python system/verify_maintainability.py --strict` PASS |

## 2. 已完成的关键改造

### 2.1 后端应用装配

- `backend/app/main.py` 已收敛为 12 行兼容入口，只负责再导出 `app.bootstrap.app` 和 `app.compat` 兼容面。
- FastAPI 应用生命周期、中间件、路由挂载统一在 `app/bootstrap.py`。
- 旧调用方通过 `app/compat.py` 保留导入兼容；`backend/app/domains` 已无 `from app.main import` 反向依赖。

### 2.2 候选域拆分

- `candidates/service.py` 收敛为 95 行门面，保留旧导入面。
- 拆出：
  - `samples.py`
  - `cleaning_sync.py`
  - `ai_review.py`
  - `candidate_queries.py`
  - `approvals.py`
- `candidates/ai_review.py` 当前 1112 行，是下一个待继续拆分的最大后端模块。

### 2.3 规则智能体域拆分

- `rule_agent/service.py` 收敛为 91 行门面，保留旧导入面。
- 拆出：
  - `agent_gateway.py`
  - `canonicalization.py`
  - `catalog.py`
  - `discovery.py`
  - `lifecycle.py`
  - `proposals.py`
  - `queries.py`
  - `reasoning.py`
  - `replay.py`
- `rule_agent/router.py` 仅保留路由编排，不再承载大块业务逻辑。

### 2.4 共享流水线与连接收口

- 新增 `system/pipeline/entrypoint.py`，提供 `run_single_step()` 单步执行封装。
- 统一 `PipelineContext`、manifest、幂等键、断点续跑和正式发布能力门禁。
- 全部 `verify_*.py`、`replay_*.py` 已不再直接调用 `sqlite3.connect`。
- 8 个 `build_*.py` 和 5 个 `publish_*.py` 已迁移到 `connect_readonly` / `connect_local`。
- 顶层脚本中直接 `sqlite3.connect` 的数量已降至 20 个。

### 2.5 测试与门禁

- 新增 `candidates/ai_review` 与 `rule_agent` 纯逻辑单元测试。
- 新增“verify/replay 脚本不再使用裸 `sqlite3.connect`”的静态回归测试。
- 后端测试文件 32 个，system 测试文件 4 个。
- 当前全量测试 **164 passed / 4 skipped**。

### 2.6 前端

- `frontend/src/App.tsx` 已采用路由级懒加载。
- 开发环境可用 `npm run check`，CI/正常终端用 `npm run build` 验证完整生产包。

## 3. 当前维护性预算

预算定义在 `contracts/maintainability_budget.json`，当前关键约束：

| 指标 | 预算 |
|---|---:|
| `backend_main_lines` | ≤ 2200 |
| `largest_backend_module_lines` | ≤ 2300 |
| `largest_system_script_lines` | ≤ 1400 |
| `system_scripts` | ≤ 100 |
| `direct_sqlite_connect_scripts` | ≤ 30 |
| `direct_duckdb_connect_scripts` | ≤ 7 |
| `pipeline_users` | ≥ 50 |
| `backend_test_files` | ≥ 26 |
| `system_test_files` | ≥ 3 |

## 4. 下一步工作

1. 继续拆分 `candidates/ai_review.py`（1112 行）。
2. 将剩余 20 个直接 `sqlite3.connect` 的运维/回填/激活类脚本迁移到 `pipeline.contracts`。
3. 清理 `app.compat` 兼容导出，待旧调用方全部迁移后删除桥接层。
4. 继续补充正常执行、幂等重跑、脏数据隔离三类固定测试。
5. 将 `semantic_registry` 和 `namespace.json` 接入标准 CI。
6. 后续逐步提升覆盖率，进入运行资产可观测性建设。

## 5. 验证命令

```powershell
# 维护性预算
python system\verify_maintainability.py --strict

# 全量测试（backend + system）
.\backend\run_tests.ps1

# 空白错误检查
git diff --check
```
