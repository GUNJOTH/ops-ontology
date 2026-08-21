# 语义工程可维护性审计报告

> 基线日期：2026-08-21；本文前半部分保留历史审计快照，最新实测以文末 S2 结果和
> `python system/verify_maintainability.py --strict` 为准。
> 方法：对 `backend/app`、`system/`、测试、依赖、CI、Git 历史做静态体检，并与长期维护型项目的通用工程基准对比。所有数据均为本仓库实测。

---

## 1. 现状快照

| 维度 | 实测值 | 说明 |
|---|---|---|
| 后端应用层 | `backend/app/main.py` **8978 行**（68 个带装饰器函数，其中 66 个为 `@domain_get/@domain_post` 路由端点；另 111 个普通函数；57 条顶层 import） | God Module |
| 域模块 | 6 个域；metadata / semantic / system 已"域自持有"，candidates / cleaning / ai_review 仍为 15–22 行的薄路由表 | 迁移进行中 |
| 系统脚本层 | `system/` 90 个脚本、**17960 行**；`build_canonical_semantic_model.py` 1273 行 / 48 函数 | 第二个 God Module + 脚本蔓延 |
| 共享框架采纳 | 26/90 脚本使用 `pipeline` 框架；**64/90 自带 `sqlite3.connect` 样板** | 重复度高 |
| 测试 | 26 个测试文件、1162 行；`system/` 90 个脚本仅 2 个测试文件（96 行） | 覆盖率约 4% |
| CI | `.github/` **为空目录，零工作流** | 无任何自动门禁 |
| 静态检查 | 无 pyproject / ruff / mypy / flake8 配置 | 无 lint、无类型检查 |
| 依赖 | 有 `backend/requirements.lock`；Python 依赖统一安装到 `backend/.deps`，backend 与 system 共用一份 | 可复现性中（仍无 pyproject/uv.lock） |
| 契约治理 | `semantic_registry.py` 单一词汇表 + `validate_registry_contract()`、版本化 schema 迁移、幂等键、审计事件、只读安全门禁、声明式 DAG | **亮点** |
| Git 历史 | **全仓仅 4 个提交**，单分支 | 无增量历史 |

---

## 2. 五维健康度评估

### 2.1 模块化与单一职责 —— 不合格（正在修复）

- **`main.py` 8978 行**是典型 God Module：路由、业务、SQL、Pydantic 模型、连接包装、启动逻辑全在一个文件。业界通用基准是模块控制在数百行内、单一职责（如 FastAPI 官方示例与 Clean Architecture 分层）。
- **`system/` 90 个散装脚本**按 `build_*`(17)、`generate_*`(14)、`verify_*`(14)、`replay_*`(11)、`publish_*`(6) 等前缀组织，本质是"按动词命名的一等公民"，彼此边界模糊。
- 已有好转：域抽取模式已验证（metadata → semantic/system），`pipeline/dag.py` 是小而清晰的声明式 DAG 框架（dataclass + 类型标注）。

### 2.2 耦合与依赖方向 —— 中（方向正确）

- 依赖方向基本健康：`domains/*` 只依赖 `app.core.*` / `app.schemas.*`，**没有任何域模块反向 import `app.main`**（实测）。
- 已把共享鉴权（`app/core/auth.py`）与请求模型（`app/schemas/*`）从 main.py 抽出，方向正确。
- 遗留问题：`_DOMAIN_ROUTE_SPECS` / `@domain_get` / `@domain_post` 过渡机制与 main.py 里的遗留 schema 声明（如 3253 行 `CanonicalSparqlRequest` 旧声明）尚未退役；candidates/cleaning/ai_review 的字符串键 handler 字典仍依赖 main.py 的 import 面。

### 2.3 可测试性与质量门禁 —— 严重不足

- 1162 行测试 vs 约 3 万行 Python，覆盖率约 4%；**90 个系统脚本只有 1 个有对应测试**。
- **零 CI**：`.github/` 为空。任何提交无自动验证，回归风险完全依赖人工。
- 无 lint / 类型检查 / 覆盖率门槛；`except Exception` 等宽泛异常在 main.py 有 3 处（系统层未统计）。
- 仓库根部散落 `pytest-cache-files-*`、`.vite/` 等运行残留（已配置 `-p no:cacheprovider` 但旧残留未清）。

### 2.4 构建与依赖可复现性 —— 弱

- 早期无 lock，现已用 `backend/requirements.lock` 固定传递依赖；仍无 pyproject/uv.lock，`fastapi>=0.115,<1`、`openai>=1,<2` 等声明区间仍用于后续升级。
- 已统一为 `backend/.deps` 单份依赖；`system/.deps`、根目录 `.deps`、`.deps_121`/`.deps_latest` 已清理；`system/requirements.txt` 已废弃，不再单独安装。
- 无打包元数据（无 pyproject）：依赖 PYTHONPATH + `.deps` 手装，新机器复现步骤隐式散落在 `run_tests.ps1` 等脚本里（有文档但未自动化到可一键复现）。

### 2.5 数据与契约治理 —— 优秀（本系统最大资产）

- 单一语义词汇表 `semantic_registry.py` + `validate_registry_contract()` 把关系键、RDF 谓词、对象类绑定在一处，杜绝各行其是。
- 版本化 schema 迁移（`semantic_schema_migration`）、幂等键（content_hash）、审计事件、`assert_safe_result` 只读/不发布安全门禁——这些是很多商业项目都没有的治理强度。
- 声明式流水线 `semantic_closure.json` + `pipeline/dag.py`，支持 `--resume-manifest` 断点续跑。
- 文档文化好：`docs/` 有 PROJECT_STANDARD、STANDARD_SEMANTIC_ENGINEERING_REQUIREMENTS、WORLD_MODEL_ARCHITECTURE 等 7 份。

---

## 3. 行业对比基准

以下基准来自业界长期维护项目的通用实践（Clean Architecture / DDD 战术模式 / Google 工程实践 / Twelve-Factor App / Microsoft 可维护性指数等）：

| 实践 | 行业成熟做法 | 本仓库现状 | 差距 |
|---|---|---|---|
| 模块规模 | 模块 ≤ ~1000 行；大文件按职责拆分 | main.py 8978 行、build_* 脚本 1273 行 | 大 |
| 入口组织 | 单一 CLI（typer/click 子命令）或 DAG 声明式编排 | 90 个散装脚本，仅 26 个走 pipeline | 大 |
| 测试金字塔 | 单测为主 + 集成 + 端到端；核心逻辑覆盖率 ≥ 70% | 覆盖率 ~4%；脚本层几乎无测试 | 大 |
| CI | 每次提交跑 lint + 类型检查 + 测试 + 覆盖率门槛 | 无 CI | 大 |
| 静态分析 | ruff/flake8 + mypy/pyright + pre-commit | 无 | 大 |
| 依赖锁定 | lock 文件 + 传递依赖固定 + 安全扫描 | 有 `backend/requirements.lock`；无 pyproject/uv.lock、无安全扫描 | 中 |
| 配置管理 | Twelve-Factor：配置经环境变量、启动时读取 | 部分在 import 时读 env（如 SEMANTIC_API_TOKEN） | 中 |
| 契约治理 | schema registry + 校验 + 版本化迁移 | semantic_registry + 版本化迁移 | **领先** |
| 安全边界 | 源只读、审计、幂等、最小权限 | assert_safe_result + 审计 + 幂等键 | **领先** |
| Git 历史 | 原子提交、可 bisect、PR 评审 | 全仓 4 个提交 | 大 |

结论：**治理层（数据/契约/安全/文档）是亮点，工程层（模块化/测试/CI/依赖/历史）是短板**。当前最危险的不是代码写得差，而是"没有门禁的大改动"：8978 行的 main.py 加上零 CI，任何一次重构都可能无声回归。

---

## 4. 问题分级清单

### P0（阻塞长期演进，应立即处理）

- **P0-1 零 CI**：`.github/` 为空。→ 建最小 CI：全量 pytest + ruff + 覆盖率报告，PR 必跑。
- **P0-2 main.py God Module**：8978 行。→ 继续已定路线：candidates/cleaning/ai_review 域迁移 + 66 个装饰端点收编 + 退役 `_DOMAIN_ROUTE_SPECS`。
- **P0-3 脚本层无门禁**：90 脚本仅 1 个有测试。→ 先给 pipeline 框架已覆盖的 26 个脚本补黄金路径/幂等/脏数据三类测试（计划文档第 5 条已列）。

### P1（可维护性收益最大）

- **P1-1 测试覆盖**：核心路径（semantic_registry、schema 迁移、审批/发布、SPARQL 门禁）已有，但域 handler 与脚本无覆盖；以"迁移一个域补一组测试"为节奏。
- **P1-2 依赖可复现**：已用 `backend/requirements.lock` 作为等价 lock，统一 backend/system 到 `backend/.deps`，并清理 `.deps_121`/`.deps_latest`/根目录 `.deps`；后续可选补 `uv.lock`/pyproject。
- **P1-3 脚本收敛**：以 `run_semantic_closure.py` 为范式，把散装脚本逐步登记进 DAG/CLI，退出条件是"新增业务步骤不新增入口脚本"。
- **P1-4 过渡机制退役**：删 main.py 遗留 schema 声明、`@domain_get/@domain_post`、`_DOMAIN_ROUTE_SPECS`，保留别名桥。
- **P1-5 Git 卫生**：约定原子提交 + conventional commits（`feat:`/`fix:`/`refactor:`）；历史已无法重写，但新提交粒度要小。

### P2（质量打磨）

- **P2-1 import 时读 env**：`SEMANTIC_API_TOKEN` 等改为启动时注入/显式配置对象，便于测试与配置审计。
- **P2-2 仓库杂物清理**：根目录 `pytest-cache-files-*`、`.vite/`、`runtime-logs/` 加入 .gitignore 或清理；检查 `.gitignore` 覆盖。
- **P2-3 文档同步**：7 份文档随迁移节奏更新（已完成 plan 文档），把"退出条件"写进每阶段。
- **P2-4 宽泛异常收敛**：`except Exception` 收窄为具体异常 + 稳定错误码（SPARQL/审批路径已有此风格，向全仓推广）。

---

## 5. 分阶段改进路线图

> 沿用仓库既有的 staged 风格：每阶段有明确退出条件，可独立验收，不一次性重写。

| 阶段 | 内容 | 退出条件 | 依赖 |
|---|---|---|---|
| **S1 门禁立起来** | 最小 CI（pytest 全量 + ruff + 覆盖率报告）；引入 ruff 配置并修到 0 error；补 `backend/requirements.lock`；清理 .deps 残留 | CI 全绿、lint 0 error、`backend/requirements.lock` 提交 | 无 |
| **S2 域迁移收尾** | candidates/cleaning/ai_review 迁为域自持有（复用 metadata/semantic 模式）；每域补路由契约测试 + 核心路径单测 | main.py < 2000 行；6 域全部自持有；`_DOMAIN_ROUTE_SPECS` 退役 | S1 |
| **S3 装饰端点收编** | 66 个 `@domain_get/@domain_post` 端点按域归位（world-model/decision/execution/review 等子域），删过渡机制与遗留 schema 声明 | main.py 仅剩 app 壳（启动、中间件、路由挂载）；无任何 `@domain_get` | S2 |
| **S4 脚本层收敛** | 以 pipeline 框架为唯一执行入口：新增脚本必须登记 DAG；存量按"高频/核心"优先补测试 + 迁入 | 90 脚本中 ≥ 60% 走 pipeline 或有测试；新增业务步骤零新增入口脚本 | S1 |
| **S5 质量打磨** | 配置注入化、异常收窄、覆盖率门槛（核心 ≥ 70%）、pre-commit、仓库清理 | 全量指标进 README/CI 报告，持续绿 | S2–S4 |

**建议立即启动 S1**（半天内可落地，收益即时且不触碰业务逻辑）。

---

## 5.1 S1 执行状态（已完成）

| 交付物 | 状态 | 说明 |
|---|---|---|
| `ruff.toml` | ✅ | 显式钉死规则集 E4/E7/E9/F/I（高价值低噪音），main.py 按 per-file-ignore 放行桥接模式的 E402/F811；**ruff check 0 error** |
| lint 修复 | ✅ | 175 个自动修复（import 排序/未使用导入）+ 29 个手工修复；**顺带发现并修复 3 个潜伏缺陷**（见下） |
| `backend/requirements.lock` | ✅ | `uv pip compile` 生成（105 项全锁）；`run_tests.ps1`/`install_backend.ps1` 改从锁安装，锁缺失时回退 |
| 本地门禁 | ✅ | `run_tests.ps1` 新增 ruff 门禁：lint 不干净即失败，不再直接跑测试 |
| CI | ✅ | `.github/workflows/ci.yml`：push/PR 跑 ruff + 全量 pytest + 覆盖率报告（上传 artifact）；CI 上 data/ 为空 → 数据依赖测试按设计跳过 |
| .deps 残留清理 | ✅ | 删除 `system/.deps_121`、`system/.deps_latest`、`system/.deps`、根目录 `.deps`；所有 Python 代码统一使用 `backend/.deps`；`system/requirements.txt` 改为废弃兼容提示 |
| 验证 | ✅ | 全量套件 **107 passed / 7 skipped / 0 failed**；ruff 0 error |

### lint 过程中发现的潜伏缺陷（均已修复）

1. **`rule_agent_payload` 重复键**（main.py）：prompt dict 里 `output_schema` 出现两次，第一块（中文标签）是死内容——Python 只保留后一块。删除第一块，行为不变。
2. **`formal_approval_queue` 死代码**（main.py）：`return` 之后有一段访问 duckdb 的不可达代码，引用未定义变量 `candidate_id`，还把列表当 dict 用——复制粘贴残留，已删除。
3. **`generate_next_ai_format_preview.py` 缺 `import sqlite3`**：脚本用 `sqlite3` 但从未导入，运行即 NameError，已补。

### 过程中一个教训（已固化）

ruff `--fix` 会把"纯再导出"的桥接 import 当未使用删除：main.py 底部的兼容别名桥（`from app.domains.*.router import ...`）被删，导致 `from app.main import metadata_catalog / canonical_semantic_sparql` 的既有测试失败。修复方式：**显式 `__all__` 声明导出面**（ruff 尊重 `__all__`）——这本身就是更规范的再导出写法。类似教训已写进 ruff.toml 注释。

### 未纳入 S1（有意为之）

- `ruff format`：156 个文件待格式化，会产生大规模机械 diff，与本次改动混在一起难以评审——留作独立提交（建议 S5 或单独一次 style 提交）。
- 根目录 `.deps`（仓库根级）已删除：统一依赖收口完成，后续不要再创建。

## S2 增量维护改进（本次实测）

在 S1 门禁立起后继续做模块化收尾与可移植性修复，当前快照已更新为：

| 维度 | 当前实测 | 说明 |
|---|---|---|
| `backend/app/main.py` | **2137 行** | 已从 8978 行显著下降；遗留桥接导入与 `_DOMAIN_ROUTE_SPECS` 仍在退役中 |
| `system/` 脚本 | **96 个 / 18661 行** | 脚本收敛仍是最主要工程债；`pipeline` 使用 37/96，`sqlite3.connect` 样板 68/96 |
| 新增域内 God Module | `candidates/service.py` **1884 行** | 路由薄了，但 handler 集中到 service，下一阶段应按 handler 继续拆分 |
| 测试 | **32 个测试文件 / 1590 行** | 全量 **140 passed / 4 skipped / 0 failed**；ruff 0 error |

### 本次修复的维护性问题

1. **域迁移导致 import 断裂**：`candidates/service.py` 在从 `main.py` 抽取后缺失 `Query/Depends/Literal/csv/re/uuid/Counter`、AI Agent 客户端、配置、Schema 等导入，导致全量测试在收集阶段即 `NameError`。已补齐导入并让域自持有依赖，不再依赖 `app.main`。
2. **反向依赖 / 循环风险**：`formal_batch_approve` 仍调用 `cleaning_registry_map/sync_cleaning_runs`，而这两个函数原在 `main.py`。已把 `cleaning_registry`、`cleaning_registry_map`、`sync_cleaning_runs` 下沉到 `candidates/service.py`，`main.py` 改为从域导入，消除 service→main 的反向依赖。
3. **RDF manifest 绝对路径不可移植**：旧 `canonical-runs/*/projection_manifest.json` 记录的是旧机器绝对路径，`rdflib.parse(str(path))` 在路径含中文/仓库移动后把 `D:\...` 当 URL 解析失败。已做两层处理：**源头**上 `build_canonical_semantic_model.py` 和 `replay_owl_rl.py` 改为写相对 run 目录的路径（如 `canonical.trig`、`../../standards/...`）；**兼容**上新增 `resolve_artifact_path()`，对旧 manifest 的失效绝对路径回退到当前仓库 `canonical-runs/<run_id>/` 下的同名产物，`verify_canonical_semantic_model.py`、`verify_standard_semantic_ci.py`、`replay_owl_rl.py` 统一使用该 helper。
4. **测试门禁真实暴露回归**：上述问题在 S1 的 CI/本地门禁中会直接红灯；本次以"先修到全绿"为退出条件，确认门禁有效。
5. **CLI 入口收敛**：`system/cli.py` 从手工重复的 6 个前缀命令重构为数据驱动注册，覆盖全部 22 个脚本前缀；后续新增 `xxx_*.py` 家族无需再改 CLI 代码。

### 下一步（S3/S4 候选）

- `candidates/service.py` 按职责拆分：AI review 决策、候选列表、正式审批、清洗同步各成一模块；
- `system/cli.py` 已改为数据驱动，覆盖 `acceptance/activate/ai/archive/backfill/build/confirm/create/diagnose/enqueue/execute/generate/init/ontology/promote/publish/replay/run/semantic/test/update/verify` 全部脚本前缀；新增业务步骤优先登记到 `pipelines/semantic_closure.json`；
- `canonical-runs` 新生成的 projection/inference manifest 已改为相对 run 目录；后续如仍有 `reports/` 或其它 JSON 写入绝对路径，按同一 `_manifest_path`/`resolve_artifact_path` 模式收敛。

## 2.6 2026-08-21 维护性门禁刷新

本次在不打开数据库、不修改运行数据的前提下重新测量，并把预算写入
`contracts/maintainability_budget.json`，由 CI 执行 `verify_maintainability.py --strict`。

| 指标 | 当前值 |
|---|---:|
| `backend/app/main.py` | 2139 行 |
| 最大后端模块 | `backend/app/domains/rule_agent/router.py`，2224 行 |
| 最大 system 脚本 | `system/build_canonical_semantic_model.py`，1304 行 |
| system 顶层脚本 | 97 个 |
| 使用 pipeline 的顶层脚本 | 32 个 |
| 仍直接调用 `sqlite3.connect` 的顶层脚本 | 56 个 |
| 后端测试文件 | 28 个 |
| system 测试文件 | 4 个 |
| 静态检查 | Ruff 通过 |
| 测试 | 138 passed / 4 skipped |

当前最需要继续收敛的不是业务表，而是三个代码边界：

1. 将 `rule_agent/router.py` 和 `candidates/service.py` 按“路由、查询、决策、外部模型适配”拆成小模块；
2. 将剩余直接数据库连接的脚本迁移到 `semantic_lib`，每批补正常、幂等、脏数据隔离测试；
3. 将前端构建、维护性预算、Ruff、pytest 和标准语义验证统一作为 CI 产物，而不是只在本地运行。



---

## 附：本报告依赖的实测命令

- 行数/函数/import：`ast` 静态解析 `backend/app/main.py`、`system/build_canonical_semantic_model.py`
- 脚本清单：`Get-ChildItem system -Filter *.py` 前缀分组
- 框架采纳：grep `from pipeline|import pipeline`（37/96）、`sqlite3.connect`（68/96）
- 测试面：`backend/tests` + `system/tests` 统计；同名 `test_<script>.py` 匹配（2/96）
- CI/配置：`Get-ChildItem -Recurse` 匹配 pyproject/ruff/mypy/.github/uv.lock
- Git：`git log --oneline | wc`（4）、`git branch -a`（单分支）
