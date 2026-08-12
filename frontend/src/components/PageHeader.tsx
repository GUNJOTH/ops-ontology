import { Space, Tag, Typography } from 'antd'

export function PageHeader({ title, description, extra }: { title: string; description: string; extra?: React.ReactNode }) {
  return <div className="page-header"><div><Typography.Title level={2}>{title}</Typography.Title><Typography.Paragraph type="secondary">{description}</Typography.Paragraph></div><Space wrap>{extra}</Space></div>
}

export function ReadOnlyTag() {
  return <Tag color="success">● 源库只读</Tag>
}
