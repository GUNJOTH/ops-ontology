# 统一业务 Action 语义层

本文档落实“连接行动”的工程化约定：**业务 Action 与系统实现解耦**。

```text
Fact
  -> Reasoning / Rule Decision
  -> Action（业务上要做什么）
  -> 前置条件 / 权限 / 输入 Fact
  -> 适配器映射（MCP / Workflow / API）
  -> 执行结果验证
  -> 成功产生 New Fact / 失败记录 Action Failed
```

## Action 的 6 个问题

项目中的每条 Action 定义都需要回答：

1. 这个 Action 是什么？
2. 什么时候允许执行？
3. 需要哪些输入 Fact？
4. 谁/哪个系统有权执行？
5. 具体映射哪个 MCP、Workflow 或 API？
6. 执行成功后会产出什么 New Fact？

## 本地实现

- `system/build_semantic_action_catalog.py` 构建 `semantic_action_definition` 目录。
- `system/verify_semantic_action_catalog.py` 校验 Action 目录完整性。
- `semantic_action_definition` 保存：
  - `action_id` / `action_key`：稳定业务动作标识，例如 `ACTION_CREATE_DEFECT`
  - `business_meaning`：业务含义，不绑定系统字段
  - `allowed_when_json`：前置条件
  - `required_facts_json`：输入 Fact
  - `permission_scope_json`：权限范围
  - `adapter_mappings_json`：MCP / Workflow / API 映射（默认 disabled）
  - `effects_json`：执行成功后的 New Fact / 对象变化
  - `execution_states_json`：`requested -> executing -> succeeded/failed`
- `semantic_action_execution` 记录本地执行状态机；`semantic_execution_adapter` 只登记适配器，不自动启用外部执行。
- `build_decision_action_layer.py` 生成 `semantic_action_plan` 时会把 Action 定义嵌入 payload，便于审批页同时看到“业务含义、前置条件、权限和效果”。

## 安全边界

- Action 目录本身不代表执行授权。
- 外部适配器默认 `disabled`，`source_write=0`、`formal_publication=0`。
- 需要人工审批的 Action 必须生成独立审批凭据后才可以进入执行准备。
- 执行结果必须以系统回执/New Fact 验证，不能仅凭“已调用工具”判定成功。
