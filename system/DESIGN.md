# 设备描述统一语义系统设计

## 1. 目标

将现有脚本和 CSV 流程收敛为可持续使用的 Python 系统，完成设备数据质量治理、统一描述候选生成、证据校验、人工审核、规则学习、回归测试和受控发布。

第一期只覆盖设备身份与设备描述。MaxiEAM/达梦源表只读，位置、KKS、分类、规格和父子关系都是支持证据，不作为独立本体或知识图谱。

## 2. 架构

```text
MaxiEAM / DM（只读）
        |
        v
快照与质量流水线（Python）
        |
        +---------------------------+
        |                           |
        v                           v
SQLite 业务主库                DuckDB 分析库
批次/规则/候选/审核/发布         全量宽表/上下文/质量报表
        |                           |
        +-------------+-------------+
                      v
            FastAPI + Jinja2/HTMX
```

SQLite 决定“这条记录处于什么业务状态”；DuckDB回答“这批数据整体表现如何”。任何审核和正式发布只能写 SQLite。

## 3. 核心业务状态

候选状态：

```text
candidate ------> approved/modified ------> published
    |                     |
    |                     +--> evaluation_case
    +--> rejected --------+--> evaluation_case
    +--> deferred
needs_review --> 人工审核
blocked ------> 不允许批准
```

术语规则状态：

```text
draft -> candidate -> confirmed -> active -> disabled/retired
```

只有 `active` 规则参与全量候选生成。规则从 `confirmed` 升为 `active` 前，必须通过所有活动回归案例。

## 4. 数据分层

- 来源层：源连接和不可变快照登记。
- 质量层：排除记录及其原因码，绝不删除审计证据。
- 身份层：`source_schema + site_id + asset_number` 唯一。
- 候选层：每个批次每台设备最多一个候选。
- 规则层：术语、缩写、单位、格式、分类和 KKS 规则。
- 校验层：合同、身份、证据、语义、上下文一致性五类验证。
- 审核层：审核决定、修改描述、原因和审核凭据。
- 回归层：人工结果转化为测试案例。
- 发布层：只有批准且回归通过的结果进入正式表。
- 审计层：关键状态变化都记录事件。

## 5. 第一版页面与接口

### 页面

- `/dashboard`：批次数量、排除量、候选状态、站点分布和上下文覆盖。
- `/batches`：批次列表、输入哈希、规则版本和校验结果。
- `/candidates`：按电厂、描述、KKS、位置、分类和状态检索。
- `/candidates/{id}`：原描述、候选、上下文证据、命中规则和验证结果。
- `/review`：待审核队列，支持通过、修改、拒绝和延后。
- `/rules`：术语规则候选、确认、启用、停用和影响范围预览。
- `/replay`：回归运行及失败案例。
- `/publications`：正式结果和发布审计。

### API

- `GET /api/stats`
- `GET /api/batches`
- `POST /api/batches/import`
- `GET /api/candidates`
- `GET /api/candidates/{candidate_id}`
- `POST /api/candidates/{candidate_id}/review`
- `GET|POST /api/rules`
- `POST /api/rules/{rule_id}/confirm`
- `POST /api/replays`
- `POST /api/publications/prepare`
- `POST /api/publications/commit`

审核和发布接口必须支持幂等键，避免重复点击造成重复写入。

## 6. 实施阶段

### 阶段 A：数据库和导入（已完成）

- 建立 SQLite 和 DuckDB；
- 导入 382,785 条当前候选；
- 建立质量查询视图和完整性校验；
- 不发布、不回写源库。

当前初始化状态见 `STATUS.md`。DuckDB 固定使用 v1.5.5，当前批次已完成双库一致性校验。

### 阶段 B：只读看板和检索

- FastAPI 服务；
- 批次和质量看板；
- 候选列表、设备上下文详情；
- DuckDB 聚合查询。

### 阶段 C：规则和样本审核

- 术语规则管理；
- 导入现有 300 条分层样本；
- 审核凭据、修改原因和并发控制；
- 人工修改自动生成回归案例。

### 阶段 D：规则执行和回归门禁

- 激活规则的确定性执行器；
- 规则影响预览；
- 全量候选再生成；
- 回归失败阻断规则启用。

### 阶段 E：正式结果层

- 准备发布与提交发布分离；
- 发布幂等、身份冲突阻断；
- 正式结果只写独立表；
- 后续如需回写 MaxiEAM，另建审批、备份和回滚流程。

## 7. 关键门禁

- 输入行数等于唯一设备身份数；
- SQLite 候选数与 DuckDB 分析事实数一致；
- 所有候选都有来源哈希、上下文哈希、规则和验证器版本；
- `blocked` 不能批准；
- 未审核候选不能发布；
- 活动回归案例全部通过才能启用新规则或发布；
- 正式设备身份冲突必须阻断，不能静默覆盖。
