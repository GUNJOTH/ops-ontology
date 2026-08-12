# 语义工程 FastAPI 后端

## 安装依赖

```powershell
Set-Location 'D:\项目\同海\semantic-engineering\backend'
.\install_backend.ps1
```

## 启动

```powershell
.\start_backend.ps1
```

默认地址：`http://127.0.0.1:8000`

开发调试使用热更新：

```powershell
.\start_backend.ps1 -Reload
```

保存 `app` 目录下的 Python 文件后，Uvicorn 会自动重启服务。`-Reload` 只用于开发，不用于生产启动。

接口：

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/candidates?page=1&page_size=50&quick_filter=pending`
- `GET /api/candidates/{candidate_id}`
- `GET /api/review-sample`
- `GET /api/candidates?page=1&page_size=50&quick_filter=pending&sample_only=true`
- `POST /api/reviews`

查询接口从 SQLite / DuckDB 读取；审核接口只写 SQLite 流程库的审核、状态和审计表，不写回 MaxiEAM。

首次访问 dashboard 或 review-sample 接口时，会为当前批次创建固定的 `high-quality-300-v1` 分层样本。样本按 `SITEID + CLASSIFICATION_DESCRIPTION` 轮询抽取，样本清单和抽取策略均写入 SQLite，保证后续审核可重复追踪。
