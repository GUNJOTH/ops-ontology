# HD_SAAS 单库语义试点

这是统一语义工程的第一单库试点，范围限定为 DM8 `HD_SAAS`。

连接源库前设置以下本地环境变量；不要把账号、密码、内网地址或 DM8 客户端路径写入代码：

```powershell
$env:HD_DM_USER = 'HD_SAAS'
$env:HD_DM_PASSWORD = '<local-secret>'
$env:HD_DM_DSN = '<dm8-host>:<port>'
$env:DMDBMS_BIN = '<local-dmdbms-bin>'
```

所有源库连接均使用只读模式，快照和导出结果默认保存在本地并被 Git 忽略。

## 边界

- 源库只读，不修改 MaxiEAM 或 DM8 表。
- 先完成 `ASSET` 设备快照和身份校验，再生成设备描述候选。
- `LOCATIONS`、`LOCHIERARCHY`、分类、规格、特征和显式关系只作为设备上下文。
- 规则和契约复用上级目录的版本；任何候选必须带来源、规则、验证器和证据。
- 该试点不创建本体、图谱、TTL 或 RDF 文件。

## 当前状态

`HD_SAAS` 是试点库，不代表两个库已经合并。HD 通过后，复制同一规则到 `XNY_SAAS`，再单独处理跨库差异。

## 目录约定

- `source_profile.yaml`：单库连接、对象范围和快照策略。
- `snapshot/`：只读源快照及其 manifest。
- `candidates/`：设备描述候选及验证结果。
- `review/`：人工复核和批准记录。
- `reports/`：单库预检与发布门禁报告。
