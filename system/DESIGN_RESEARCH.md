# 页面设计学习记录

日期：2026-08-12

## 参考来源

- [Ant Design Table](https://ant.design/components/table/)
- [IBM Carbon Data table usage](https://carbondesignsystem.com/components/data-table/usage/)
- [Microsoft Fluent 2 Searchbox](https://fluent2.microsoft.design/components/web/react/core/searchbox/usage)
- [Microsoft Fluent 2 Nav](https://fluent2.microsoft.design/components/web/react/core/nav/usage)
- [Material 3 canonical layout examples](https://m3.material.io/foundations/layout/canonical-examples/overview)

## 对本系统的落地

| 学习结论 | 本系统的设计决定 |
| --- | --- |
| 数据表适合排序、搜索、筛选、分页，长列表可使用固定表头或虚拟化 | 候选数据只服务端分页，默认 50 条，表头固定，后续再接虚拟滚动 |
| 表格工具栏承载搜索、筛选、设置和少量全局动作 | 工具栏只保留搜索、快捷筛选、密度和导出；其他动作收进更多菜单 |
| 批量模式要有清晰的选中态和批量动作栏 | 选中后显示作用域与数量；高风险批准默认不跨证据组批量执行 |
| 行可以展开或进入侧面板，复杂内容再进入独立页面 | 快速查看用右侧抽屉，修改与审计用完整审核页 |
| 导航和布局要围绕任务、列表-详情关系组织 | 一级入口按工作台 / 治理分组，审核页采用列表-详情结构 |
| 搜索应支持清除、筛选和键盘操作 | 支持 ASSETNUM、KKS、原描述搜索，保留筛选标签和快捷键提示 |

## 本轮明确不采用

- 不做大面积深色背景、渐变装饰和只靠图表表达状态。
- 不在首屏展示过多实现细节；规则版本、哈希和原始上下文放到详情的审计区。
- 不把 38 万条数据一次性渲染到浏览器。
- 不把“自动生成”视觉上包装成“已批准”；候选、审核、批准、发布始终分开显示。
