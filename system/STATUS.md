# 系统初始化状态

更新时间：2026-08-12

状态：`PASS`

## 已完成

- SQLite 业务库已初始化：`data/semantic_workflow.sqlite3`。
- DuckDB 分析库已初始化：`data/semantic_analytics_v155.duckdb`。
- DuckDB 版本：`1.5.5`。
- 当前批次：`hd-semantic-candidate-generation-20260812T012800Z`。
- 输入、候选、设备身份均为 382,785 条。
- SQLite 外键检查通过。
- DuckDB 上下文 JSON 无法解析记录为 0。
- 覆盖 10 个电厂。
- 正式发布记录为 0，未审核结果未进入正式层。
- 源 MaxiEAM/达梦表未写入。

## 错误修复

1. 初始 DuckDB 1.3.2 在本机 Python 3.12 加载原生扩展时发生 Windows access violation；经过最小连接测试确认是运行时兼容性问题，不是业务数据问题。
2. 按要求验证官方最新 DuckDB v1.5.5，当前环境导入、内存连接和全量建库均通过，因此系统固定使用 v1.5.5。
3. 分析视图原先引用不存在的顶层 `LOCATION_CODE` 字段；当前批次的 KKS/位置码实际位于 `CONTEXT_JSON.location.LOCATION`，已修正为从 JSON 上下文读取。
4. 逐行写入速度过慢，已改为 DuckDB 原生批量导入；全量建库成功。

## 当前边界

- 已提供 Web 审核页面，审核接口只写 SQLite 审核与审计表。
- 382,785 条候选当前仍是 `candidate_only`，没有正式发布。
- 当前没有已确认的术语规则，因此候选描述保留原描述。
- 已创建 `high-quality-300-v1` 固定分层样本：按 `SITEID + CLASSIFICATION_DESCRIPTION` 轮询抽取 300 条。
- 样本审核完成后，再根据批准、修改、拒绝结果形成回放评估集；当前尚未提交任何审核决定。
