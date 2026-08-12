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

- 目前是数据库和批次导入骨架，还没有开放 Web 审核页面。
- 382,785 条候选当前仍是 `candidate_only`，没有正式发布。
- 当前没有已确认的术语规则，因此候选描述保留原描述。
- 已提供批次看板、候选检索、上下文详情和审核抽屉；当前仍未正式发布候选。
