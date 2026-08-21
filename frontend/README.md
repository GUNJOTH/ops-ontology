# 语义工程前端

## 启动

首次运行：

```powershell
Set-Location 'D:\项目\同海\ops-ontology\frontend'
npm install
```

正常启动：

```powershell
.\start_frontend.ps1
```

脚本直接调用 `node.exe` 启动 Vite，不通过 `cmd.exe` 或 `npm.cmd`，用于规避部分 Windows 环境的 `0xc0000142` 启动弹窗。

打开：`http://127.0.0.1:5173/`

Vite 默认启用 HMR。修改 `src` 下的 React、TypeScript 或 CSS 文件后，浏览器会自动刷新或保留状态更新。

也可以从工程目录运行 `..\start_dev.ps1`，同时启动 FastAPI 热更新和 Vite HMR。

## 构建

```powershell
npm run build
```

## 当前页面

- `/`：批次总览，突出下一步审核动作。
- `/candidates?review=1`：候选审核工作台，支持服务端查询参数、筛选、选择、详情抽屉和审核动作。

默认只使用 FastAPI 返回的 SQLite / DuckDB 数据。项目不再提供 Mock 数据模式；API 请求失败时页面显示错误，不会静默伪造数据。
