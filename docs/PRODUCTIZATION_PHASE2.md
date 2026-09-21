# 通用企业语义智能平台 V1：产品化第二阶段

## 目标

第二阶段完成 Ontology Package 原生模块化与组合运行：

```text
Platform Core
      +
Industry Ontology Package
      ↓ Resolver
      ↓ Composition Engine
Canonical RDF Dataset
      ↓
Runtime Meta Model / SHACL / OWL RL / SPARQL / JSON-LD
```

平台 Core 不包含供热行业硬编码；供热能力由
`packages/thermal-operations/` 加载。源系统继续只读，组合过程不执行源写入和正式业务发布。

## 已实现的 P0

- `packages/platform-core/`：通用对象、事件、事实、规则、行动、来源和人工复核语义；
- `packages/thermal-operations/`：设备、位置、巡检、缺陷、工单及行业事件语义；
- `packages/manifest.json`：包版本、依赖、默认组合和安全边界；
- `system/ontology_package.py`：版本解析、依赖拓扑排序、资产安全路径、声明冲突检测和组合产物；
- `system/compose_ontology.py`：生成 Canonical ontology、SHACL、SKOS、JSON-LD context 与组合 manifest；
- `system/verify_semantic_packages.py`：归属、资产、依赖、组合和 promoted asset drift 门禁；
- `standards/v2/`：由组合产物提升的正式 v2 语义资产；
- Semantic API V2：包详情、Ontology 类/属性目录和对象事件时间线查询。

## 组合规则

1. 包 namespace 必须唯一；
2. 同一 OWL Class、ObjectProperty、DatatypeProperty、SHACL NodeShape 不得由多个包声明；
3. SKOS notation 冲突直接失败；
4. JSON-LD context 同名词项映射冲突直接失败；
5. 组合结果必须与 `standards/v2/` promoted snapshot 对齐；
6. 任一门禁失败，不得进入 Canonical Release。

## 运行命令

```powershell
python system\compose_ontology.py --output builds\canonical\v2
python system\verify_semantic_packages.py
python system\verify_standard_semantic_ci.py
python system\verify_ontology_release.py --spec standards\ontology-version-v2.json
```

## 尚未完成的 P1/P2

- 用组合后的 Ontology 自动生成 Runtime Meta Model，并减少 Registry 中的人工声明；
- 真实客户扩展包的映射、规则、事件和 SHACL 样例；
- Knowledge Core V0.1 与标准规则/行动 RDF 化；
- Semantic API 的 JSON Schema、权限、分页和性能基线；
- 多行业包兼容性回放及 release 迁移/回滚演练。
