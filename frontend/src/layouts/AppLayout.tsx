import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { ApartmentOutlined, AppstoreOutlined, BookOutlined, BranchesOutlined, BulbOutlined, ClearOutlined, DatabaseOutlined, EnvironmentOutlined, FileSearchOutlined, MenuFoldOutlined, MenuUnfoldOutlined, ReadOutlined, RobotOutlined, SearchOutlined, SettingOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Button, Input, Layout, Menu, Space, Tag, Tooltip, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'

const { Header, Sider, Content } = Layout

export function AppLayout({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [search, setSearch] = useState('')

  const selectedKey = useMemo(() => {
    if (location.pathname.startsWith('/candidates')) return '/candidates'
    if (location.pathname.startsWith('/cleaning')) return '/cleaning'
    if (location.pathname.startsWith('/ai-review')) return '/ai-review'
    if (location.pathname.startsWith('/rule-agent')) return '/rule-agent'
    if (location.pathname.startsWith('/published')) return '/published'
    if (location.pathname.startsWith('/metadata')) return '/metadata'
    if (location.pathname.startsWith('/unified-devices')) return '/unified-devices'
    if (location.pathname.startsWith('/unified-locations')) return '/unified-locations'
    if (location.pathname.startsWith('/identity-review')) return '/identity-review'
    if (location.pathname.startsWith('/ontology-runtime')) return '/ontology-runtime'
    if (location.pathname.startsWith('/knowledge-assets')) return '/knowledge-assets'
    if (location.pathname.startsWith('/semantic-facts')) return '/semantic-facts'
    if (location.pathname.startsWith('/semantic-status')) return '/semantic-status'
    if (location.pathname.startsWith('/semantic-events')) return '/semantic-events'
    if (location.pathname.startsWith('/decisions')) return '/decisions'
    return '/'
  }, [location.pathname])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        document.querySelector<HTMLInputElement>('#global-search')?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const menuItems: MenuProps['items'] = [
    { type: 'group', label: '世界模型', children: [
      { key: '/', icon: <AppstoreOutlined />, label: '运行总览' },
      { key: '/unified-devices', icon: <ApartmentOutlined />, label: '设备对象' },
      { key: '/unified-locations', icon: <EnvironmentOutlined />, label: '位置对象' },
      { key: '/identity-review', icon: <FileSearchOutlined />, label: '身份审批' },
      { key: '/semantic-events', icon: <BranchesOutlined />, label: '事件中心' },
      { key: '/semantic-status', icon: <BookOutlined />, label: '状态中心' },
      { key: '/semantic-facts', icon: <BranchesOutlined />, label: '事实 · 事理 · 行动' },
      { key: '/ontology-runtime', icon: <BranchesOutlined />, label: '本体元模型' },
      { key: '/decisions', icon: <ThunderboltOutlined />, label: '决策与行动' },
    ] },
    { type: 'group', label: '数据治理', children: [
      { key: '/candidates', icon: <FileSearchOutlined />, label: '设备候选' },
      { key: '/cleaning', icon: <ClearOutlined />, label: '清洗任务' },
      { key: '/metadata', icon: <DatabaseOutlined />, label: '元数据语义' },
    ] },
    { type: 'group', label: '规则与智能', children: [
      { key: '/knowledge-assets', icon: <BookOutlined />, label: '知识资产' },
      { key: '/rule-agent', icon: <BulbOutlined />, label: '规则发现智能体' },
      { key: '/ai-review', icon: <RobotOutlined />, label: 'AI 语义复核' },
    ] },
    { type: 'group', label: '发布与追溯', children: [
      { key: '/published', icon: <ReadOutlined />, label: '正式结果' },
    ] },
  ]

  const onSearch = (value: string) => {
    const next = value.trim()
    if (next) navigate(`/candidates?search=${encodeURIComponent(next)}`)
  }

  return <Layout className="app-layout"><Sider collapsible collapsed={collapsed} trigger={null} width={232} collapsedWidth={72} className="app-sider"><div className="brand-block"><div className="brand-mark">本</div>{!collapsed && <div><Typography.Text strong>企业运维本体</Typography.Text><Typography.Text type="secondary"> World Model</Typography.Text></div>}</div><Menu mode="inline" selectedKeys={[selectedKey]} items={menuItems} onClick={({ key }) => navigate(String(key))} className="main-menu" /><div className="sider-footer"><Tag color="success">源系统只读</Tag>{!collapsed && <Typography.Text type="secondary">HD · XNY · MaxiEAM</Typography.Text>}</div></Sider><Layout><Header className="app-header"><Space size={12} className="header-left"><Tooltip title={collapsed ? '展开导航' : '收起导航'}><Button type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(!collapsed)} /></Tooltip><Typography.Text type="secondary">运行层</Typography.Text><Tag color="blue">Ontology Runtime v0.2</Tag></Space><Space size={12} className="header-right"><Input id="global-search" prefix={<SearchOutlined />} placeholder="搜索设备、位置、状态、事实" value={search} onChange={(event) => setSearch(event.target.value)} onPressEnter={() => onSearch(search)} suffix={<Typography.Text type="secondary" className="shortcut">Ctrl K</Typography.Text>} /><Tooltip title="设置"><Button type="text" icon={<SettingOutlined />} /></Tooltip></Space></Header><Content className="app-content">{children}</Content></Layout></Layout>
}
