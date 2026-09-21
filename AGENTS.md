# 语义工程依赖规则（所有智能体必须遵守）

本仓库的 Python 依赖已经统一，不再维护多套独立依赖环境。

## 唯一 Python 依赖环境

- 依赖声明：`backend/requirements.txt`（运行时）+ `backend/requirements-dev.txt`（测试/静态检查）
- 精确基线：`backend/requirements.lock`（由 uv pip compile 生成，必须提交）
- 安装目录：`backend/.deps`
- 适用范围：**backend 与 system 的所有 Python 代码、脚本、测试和 CI**

`system/requirements.txt` 已废弃，仅保留为兼容提示。禁止再安装或引用 `system/.deps`、根目录 `.deps`、`.deps_121`、`.deps_latest`。

## 怎么安装/使用

```powershell
# 1. 安装/刷新统一 Python 依赖（幂等）
.\backend\install_backend.ps1
# 或手动执行：
# uv pip install --target backend/.deps -r backend/requirements.lock

# 2. 运行后端测试（会自动安装依赖、跑 ruff、跑 pytest）
.\backend\run_tests.ps1

# 3. 运行 system 脚本（复用 backend/.deps）
$env:PYTHONPATH = 'D:\项目\同海\ops-ontology\backend\.deps;D:\项目\同海\ops-ontology\backend;D:\项目\同海\ops-ontology\system'
python system\verify_system.py
python system\run_semantic_closure.py --help
```

## 如何新增/升级 Python 依赖

1. 运行时依赖写入 `backend/requirements.txt`；
2. 测试或静态检查依赖写入 `backend/requirements-dev.txt`；
3. 重新生成锁文件并提交：

```powershell
uv pip compile backend/requirements.txt backend/requirements-dev.txt -o backend/requirements.lock --no-header
```

4. 重新安装并验证：

```powershell
.\backend\install_backend.ps1
.\backend\run_tests.ps1
```

## 前端依赖

前端继续使用独立但标准的 Node 依赖：

```powershell
cd frontend
npm install
npm run build
```

不要将 `node_modules` 或前端依赖纳入 Python 依赖管理。

## 禁止事项

- 禁止新建 `system/.deps`、根目录 `.deps` 或任何 `.deps_*` Python 依赖副本。
- 禁止在 CI 中单独执行 `pip install -r system/requirements.txt` 或安装 `system/.deps`。
- 禁止直接 import 依赖而不先确保 `backend/.deps` 已安装并处于 `PYTHONPATH`。
