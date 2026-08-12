# 设备描述统一语义系统（SQLite + DuckDB）

本目录是设备描述 Harness 的本地可运行系统骨架。它只管理设备身份、设备描述、支持上下文、规则、审核、回归和正式结果，不建立本体、图谱或 TTL。

## 双库职责

- `data/semantic_workflow.sqlite3`：业务主库。保存来源快照、处理批次、设备身份、候选、规则、验证、审核、回归、发布和审计记录。
- `data/semantic_analytics_v155.duckdb`：分析库。使用 DuckDB v1.5.5，保存全量候选宽表和上下文 JSON，负责质量统计、站点分布、规则影响分析和报表查询。
- SQLite 是流程状态的唯一真相；DuckDB 不保存审批状态的权威副本。
- 两个数据库通过 `candidate_id`、`batch_id`、`source_snapshot_id` 对齐。

## 系统步骤

1. 从 MaxiEAM/达梦只读抽取设备快照。
2. 校验来源范围、设备粒度、身份唯一性和快照哈希。
3. 构建设备位置、KKS、分类、规格、父子关系上下文。
4. 执行停用、空描述、低重合、极短、通用、纯编码等质量规则。
5. 对高质量批次运行已启用的确定性术语与格式规则。
6. 执行合同、身份、证据、语义和上下文一致性验证。
7. 将结果路由为 `candidate`、`needs_review` 或 `blocked`。
8. 审核人执行 `approved`、`modified`、`rejected` 或 `deferred`。
9. 人工修改和拒绝沉淀为回归案例；新规则启用前必须回放通过。
10. 只有带审核凭据且回归门禁通过的结果才能进入正式结果表；源库不回写。

## 初始化

使用项目本地依赖目录运行：

```powershell
& 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe' `
  'D:\项目\同海\semantic-engineering\system\init_system.py'
```

初始化会读取当前 HD 高质量语义候选 CSV，将流程元数据与精简候选写入 SQLite，将完整宽表写入 DuckDB。

## 验证

```powershell
& 'D:\uv\python\cpython-3.12.13-windows-x86_64-none\python.exe' `
  'D:\项目\同海\semantic-engineering\system\verify_system.py'
```

验证必须满足：输入数、设备身份数、候选数相等；身份和候选 ID 无重复；上下文 JSON 可解析；没有未经审核的正式结果。

## 第一版页面

建议按以下顺序开发 Python Web 功能：

1. 批次和质量看板；
2. 候选检索与设备上下文详情；
3. 术语规则管理；
4. 300 条分层样本审核；
5. 全量审核队列；
6. 回归测试与规则版本发布；
7. 正式结果发布和审计查询。

初期推荐 FastAPI + Jinja2/HTMX。数据库访问层分别使用标准库 `sqlite3` 和 `duckdb`，避免过早引入复杂 ORM。
