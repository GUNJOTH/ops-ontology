import { useEffect, useMemo, useState } from 'react'
import { AppstoreOutlined, AuditOutlined, BookOutlined, CheckCircleOutlined, DatabaseOutlined, FileSearchOutlined, MenuFoldOutlined, MenuUnfoldOutlined, ReadOutlined, SearchOutlined, SettingOutlined } from '@ant-design/icons'
import { Button, Input, Layout, Menu, Space, Tag, Tooltip, Typography } from 'antd'
import type { MenuProps } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'

const { Header, Sider, Content } = Layout

export function AppLayout({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)
  const [search, setSearch] = useState('')

  const selectedKey = useMemo(() => {
    if (location.pathname.startsWith('/candidates')) return '/candidates'
    if (location.pathname.startsWith('/rules')) return '/rules'
    if (location.pathname.startsWith('/replay')) return '/replay'
    if (location.pathname.startsWith('/published')) return '/published'
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
    { type: 'group', label: '工作台', children: [
      { key: '/', icon: <AppstoreOutlined />, label: '总览' },
      { key: '/batches', icon: <DatabaseOutlined />, label: '批次管理' },
      { key: '/candidates', icon: <FileSearchOutlined />, label: '设备候选' },
      { key: '/candidates?quick_filter=pending', icon: <AuditOutlined />, label: '审核工作台' },
    ] },
    { type: 'group', label: '治理', children: [
      { key: '/rules', icon: <BookOutlined />, label: '术语规则' },
      { key: '/replay', icon: <CheckCircleOutlined />, label: '回归测试' },
      { key: '/published', icon: <ReadOutlined />, label: '正式结果' },
    ] },
  ]

  const onMenuClick: MenuProps['onClick'] = ({ key }) => navigate(String(key))
  const onSearch = (value: string) => {
    const next = value.trim()
    if (next) navigate(`/candidates?search=${encodeURIComponent(next)}`)
  }

  return (
    <Layout className="app-layout">
      <Sider collapsible collapsed={collapsed} trigger={null} width={232} collapsedWidth={72} className="app-sider">
        <div className="brand-block">
          <div className="brand-mark">语</div>
          {!collapsed && <div><Typography.Text strong>设备语义治理</Typography.Text><Typography.Text type="secondary"> Harness Console</Typography.Text></div>}
        </div>
        <Menu mode="inline" selectedKeys={[selectedKey]} items={menuItems} onClick={onMenuClick} className="main-menu" />
        <div className="sider-footer">
          <Tag color="success">源库只读</Tag>
          {!collapsed && <Typography.Text type="secondary">HD_SAAS · v0.1</Typography.Text>}
        </div>
      </Sider>
      <Layout>
        <Header className="app-header">
          <Space size={12} className="header-left">
            <Tooltip title={collapsed ? '展开导航' : '收起导航'}><Button type="text" icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />} onClick={() => setCollapsed(!collapsed)} /></Tooltip>
            <Typography.Text type="secondary">批次</Typography.Text>
            <Tag color="blue">hd-semantic-20260812</Tag>
          </Space>
          <Space size={12} className="header-right">
            <Input id="global-search" prefix={<SearchOutlined />} placeholder="搜索 ASSETNUM、KKS、原描述" value={search} onChange={(event) => setSearch(event.target.value)} onPressEnter={() => onSearch(search)} suffix={<Typography.Text type="secondary" className="shortcut">Ctrl K</Typography.Text>} />
            <Tooltip title="设置"><Button type="text" icon={<SettingOutlined />} /></Tooltip>
          </Space>
        </Header>
        <Content className="app-content">{children}</Content>
      </Layout>
    </Layout>
  )
}
