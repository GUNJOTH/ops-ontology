# 统一业务对象与知识身份层

## 核心原则

统一业务对象不是把所有系统数据复制到一起，而是建立稳定的业务身份和关系：

```text
业务对象：循环泵 A
  ├── 巡检事实
  ├── 缺陷事实
  └── 工单事实

知识资产：RULE-PUMP-BEARING-001
  ├── 条件
  ├── 规则
  └── Action
```

来源数据继续保留在原系统或只读快照中；本地层只保存身份、版本、来源指针、关系和审核状态。

## 表职责

| 表 | 作用 |
| --- | --- |
| `business_object_type` | 设备、位置、巡检、缺陷、工单、知识资产等业务概念目录 |
| `unified_device` | 设备业务对象的实际身份表，位于只读身份快照中 |
| `device_identity_map` | 各来源系统设备编码到设备对象的身份映射 |
| `business_record_link` | 巡检、缺陷、工单到设备对象的本地挂接 |
| `knowledge_asset` | 规则、术语、评估用例的唯一知识身份 |
| `knowledge_asset_version` | 知识版本、内容哈希、回放统计和启用状态 |
| `knowledge_asset_part` | 条件、规则、Action、证据等组成部件 |
| `knowledge_asset_source` | 知识资产的来源记录、快照和原始状态 |
| `knowledge_asset_binding` | 知识资产适用的业务对象或设备身份 |
| `knowledge_asset_issue` | 重复、差异、冲突、证据缺失问题 |

## 业务对象目录

业务对象目录已登记为关系型父子类型，但类型定义不等于已经拥有对应实例：

```text
业务对象
├── 组织
│   ├── 分公司
│   ├── 中心
│   └── 班组
├── 物理对象
│   ├── 站点
│   └── 设备
│       ├── 循环泵
│       ├── 补水泵
│       └── 换热器
├── 业务事件
│   ├── 巡检
│   ├── 异常巡检
│   ├── 缺陷
│   ├── 消缺
│   └── 工单
└── 业务知识
    ├── 标准
    ├── 规则
    ├── SOP
    └── 风险
```

当前快照能确认的设备、位置、巡检、缺陷、工单关系已进入 `business_object_relation`；组织、站点、异常巡检、消缺、SOP 和风险暂时只有类型定义，等源数据提供明确身份字段后再建立实例，避免虚构关系。

## 知识整合规则

1. 同一 `asset_key` 归并为一个知识身份，不因来源不同产生多个正式资产。
2. 不同来源的内容分别保存为来源记录和版本，不静默覆盖。
3. 冲突、重复和证据缺失进入问题表，不能直接启用。
4. `approved`、`enabled`、`retired` 和 `blocked` 分开表示，不把“已发现”当成“已生效”。
5. 正式发布仍然独立于知识资产登记；当前层不修改 DM8/MaxiEAM 源表。

## 机器可理解契约

每个知识资产版本还生成一份本地执行契约：

| 能力 | 机器规格 | 结果 |
| --- | --- | --- |
| 可判断 | `machine_decision_spec` | 输入字段、输出枚举、置信度门槛、判断表达式 |
| 可计算 | `machine_calculation_spec` | 操作、输入字段、输出字段、参数、确定性 |
| 可约束 | `machine_constraint_spec` | 身份、证据、语义、一致性、回放、审批和源写入门禁 |
| 可行动 | `machine_action_spec` | 行动类型、目标、参数、审批要求、幂等键模板 |
| 可解释 | `machine_semantic_contract` | 输入理解、判断、计算、约束和行动的完整契约 |

当前规则行动默认需要审批，回放行动可以独立执行；任何行动都声明 `source_write=0`，不能绕过正式审批直接写源库。

## 可解释事实链

业务本体要能让机器沿着明确语义、规则和约束生成可解释的新事实，因此补充了四层本地关系表：

| 表 | 作用 |
| --- | --- |
| `semantic_fact` | 记录来源事实或派生事实；每条记录保留设备对象、谓词、来源 schema、源表、源行和快照 |
| `semantic_fact_derivation` | 记录派生事实使用的规则资产、规则版本、输入事实、解释和约束结果 |
| `semantic_rule_decision` | 记录规则对设备或事件作出的判断、置信度、输入事实、解释和是否需要行动 |
| `semantic_escalation_action` | 记录升级、通知、创建工单或消缺事件的本地行动计划；默认 `source_write=0` 且需要审批 |
| `semantic_action_definition` | 统一业务 Action 资产：业务含义、何时允许、输入 Fact、权限范围、适配器映射、效果和执行状态机 |
| `semantic_action_execution` | Action 本地执行状态记录：requested -> executing -> succeeded/failed |

| `semantic_fact_layer_run` | 记录事实层构建批次及各层数量，便于回放和交付核对 |

当前事实层先登记 89 条已有 `business_object_relation` 证据的观测、缺陷和工单事件，并从只读身份快照带入事件类型、状态、描述、位置和时间等原始字段。最近一次闭环已形成 105 条可追溯的派生事实/规则判断，但由于正式快照没有明确测量值、风险证据和已确认缺陷状态，Fact Builder 未生成风险事实，行动计划仍为 0。这是安全门槛，不代表流程缺失：必须先补充真实的观测字段和规则输入，才能产生温度、缺陷等级、风险判断或行动，不得把示例文本当作生产事实。当前已有规则资产 176 条、机器契约就绪 176 条，但“规则已登记”与“规则已对事实执行”严格分开。

页面入口：`/semantic-facts`。接口为 `GET /api/semantic-facts/summary`、`GET /api/semantic-facts` 和 `GET /api/semantic-facts/{fact_id}`，均只读本地语义覆盖层。

## HD/XNY 缺陷状态字典

`system/build_defect_status_dictionary.py` 从身份语义只读快照提取 HD/XNY 缺陷状态原值、源表、证据数量、设备挂接数量和样本，写入本地 `semantic_status_dictionary`。当前形成 14 个源状态候选、覆盖 868 条缺陷记录，全部仍为 `pending`；8 个 Canonical Defect State 是独立的标准状态定义，不等于源状态映射已确认。

页面入口：`/semantic-status`。审核接口只更新本地字典的 `mapping_status`、`business_meaning` 和审核依据；未确认的状态不会进入业务规则，源表和正式结果层不变。页面上的“运行状态映射回放”只读取 `approved` 条目；当前仍为 0 条已确认源状态映射，因此“源状态字典回放”保持 `blocked`。独立的 Canonical 快照状态回放已按 8 个主体完成，二者不能混为一谈，也不会把源编码未经确认解释为业务状态或生成行动。

## 确定性事理执行器

`system/execute_semantic_reasoning.py` 是第一版本地执行器。它只接收 `semantic_fact` 中已经具备来源证据、状态为 `observed` 或 `accepted` 的事实，并执行四条存在性规则：巡检事件存在、缺陷事件存在、工单事件存在、缺陷状态字段存在。缺陷状态先作为独立原子事实保存，规则不解释状态编码。

每次执行都会写入本地：

1. `semantic_rule_decision`：记录输入事实、规则版本、判断值、置信度和解释。
2. `semantic_fact`：记录由规则产生的存在性派生事实。
3. `semantic_fact_derivation`：记录输入事实到派生事实的完整推理链和约束结果。
4. `semantic_reasoning_run`：记录批次统计和 `source_write=0`、`formal_publication=0` 门禁。

当前执行结果为 97 条输入事实、105 条规则判断、105 条派生事实、0 条行动，其中新增的 8 条是缺陷状态原子事实。行动表保持为空，是因为当前事实没有足够的阈值、风险等级或业务授权证据来安全创建工单或消缺事件。后续新增规则必须明确输入事实类型、输出事实、规则版本、解释和行动审批要求，不能直接在页面代码中隐式判断。

## 语义行动预演

`semantic_action_run` 是机器执行器的安全前置层。它读取知识版本的执行契约，计算契约完整性、回放证据、审批状态和源写入门禁，生成 `ready`、`needs_review` 或 `blocked` 的执行计划，并使用幂等键记录预演结果。当前只生成本地审计凭据，不执行源写入和正式发布。
