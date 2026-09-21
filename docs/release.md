# 发布约定

本文档规定 `ops-ontology` 的代码发布和 Canonical RDF Semantic Release 流程。两者
必须分别通过各自门禁；Git tag 或 GitHub Release 不会自动激活语义版本。

## 版本与变更记录

- 仓库代码发布使用 `MAJOR.MINOR.PATCH` 版本格式，并使用 `vMAJOR.MINOR.PATCH` 作为
  Git tag。当前仓库没有统一的后端 Python 包版本字段，仓库 tag 是代码发布版本的
  权威标识。
- `frontend/package.json` 的 `version` 只表示前端包元数据；如果发布 PR 改动它，
  必须在说明中写明是否与仓库级版本同步，不得把它单独当作完整平台版本。
- 标准语义版本由 `standards/ontology-version.json`、Canonical RDF Dataset、资源
  manifest 和本地 Semantic Release 注册表共同表达。修改本体、词表、SHACL、JSON-LD、
  SPARQL、映射或规则资产时，必须同时更新相应版本/来源证据，并经过语义发布门禁。
- 用户可见的功能、修复、API 契约、标准资产、规则、配置和迁移变化必须先在
  `CHANGELOG.md` 的对应版本条目中记录。
- 版本记录在同一个 Pull Request 中修改；PR 目标为 `main`，不得直接推送或强推 `main`。
- 尚未形成正式版本的变化写入 `[未发布]`，不要在没有实际发布内容时伪造历史版本条目。

## 发布前检查

代码或前端变更至少执行：

```powershell
.\backend\run_tests.ps1
python -m compileall backend system
npm ci --prefix frontend
npm run build --prefix frontend
```

涉及 Canonical RDF 或标准语义资产时，还必须在已准备隔离依赖和受控语义数据的环境
执行：

```powershell
python system\verify_release_gates.py --output .\system\reports\release-gates.json
```

该门禁必须同时确认 Canonical、标准 CI 和 source-of-truth 切换检查通过；输出
`BLOCKED`、缺少受控数据或只有局部门禁通过，都不能写成正式发布通过。发布前还应
保留备份、审批、激活或回滚所需的审计证据，并确认 `sourceWrite=false`、
`formalPublication=false` 的边界没有被破坏。

如果默认分支的 CI 尚未进入 `main`，必须在 PR 中附上上述实际命令和结果；CI 合并后，
以对应后端/system 测试和前端构建结果作为持续验证证据。真实源系统、生产数据库和
生产凭据不属于本地发布验证范围。

## 发布步骤

1. 从 `main` 创建版本 PR，更新 `CHANGELOG.md`；如涉及标准资产，先完成语义版本、
   manifest、来源和回滚证据的同步变更。
2. 等待后端/system 测试、前端构建和必要的语义发布门禁通过，并完成 API、标准资产、
   审批边界、敏感信息和部署风险审查后合并。
3. 在已合并的 `main` 提交上创建对应的 `vMAJOR.MINOR.PATCH` tag，并推送该 tag；不
   修改或覆盖已有 tag。
4. 基于该 tag 创建 GitHub Release，标题使用版本号，正文引用 `CHANGELOG.md` 中的
   对应条目，并明确标准语义版本、迁移、配置变化、审批状态和已知限制。
5. 如果需要激活新的 Semantic Release，按本地注册表的审批、备份、激活和回滚流程
   单独执行；代码 Release 成功不等于语义版本已经激活。
6. 发布后确认 GitHub Release、Git tag、变更记录和相关语义版本证据一致；发现问题时
   按补丁版本或新的语义版本发布修复，不回写已发布 tag。

## 回滚边界

发布回滚不得通过删除或覆盖 tag 伪造历史。应用代码、前端静态资源、Canonical RDF
Dataset、语义注册表指针、审批记录和本地运行库必须分别评估；如果语义资产或数据库
迁移不可逆，应保留原版本备份和审计证据，并通过前向修复或明确的回滚流程处理。
