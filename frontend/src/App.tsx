import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider, Empty, Typography } from 'antd'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppLayout } from './layouts/AppLayout'
import { DashboardPage } from './pages/DashboardPage'
import { CandidatePage } from './pages/CandidatePage'
import './styles/app.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 0 } } })

function PlaceholderPage({ title }: { title: string }) {
  return <div className="placeholder-page"><Typography.Title level={2}>{title}</Typography.Title><Typography.Paragraph type="secondary">该模块将在审核工作台验证通过后接入，当前不改变源 MaxiEAM 数据。</Typography.Paragraph><Empty description="功能建设中" /></div>
}

export function App() {
  return <QueryClientProvider client={queryClient}><ConfigProvider theme={{ token: { colorPrimary: '#2563eb', colorBgLayout: '#f6f8fb', borderRadius: 10, fontFamily: 'system-ui, Microsoft YaHei, sans-serif' }, components: { Layout: { headerBg: '#ffffff', siderBg: '#ffffff', bodyBg: '#f6f8fb' }, Menu: { itemSelectedBg: '#eff6ff', itemSelectedColor: '#2563eb', itemBorderRadius: 8 }, Card: { headerFontSize: 15 } } }}><BrowserRouter><AppLayout><Routes><Route path="/" element={<DashboardPage />} /><Route path="/candidates" element={<CandidatePage />} /><Route path="/batches" element={<PlaceholderPage title="批次管理" />} /><Route path="/rules" element={<PlaceholderPage title="术语规则" />} /><Route path="/replay" element={<PlaceholderPage title="回归测试" />} /><Route path="/published" element={<PlaceholderPage title="正式结果" />} /><Route path="*" element={<DashboardPage />} /></Routes></AppLayout></BrowserRouter></ConfigProvider></QueryClientProvider>
}
