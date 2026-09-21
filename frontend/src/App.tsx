import { lazy, Suspense } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppLayout } from './layouts/AppLayout'
import './styles/app.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 0 } } })

const DashboardPage = lazy(async () => ({ default: (await import('./pages/DashboardPage')).DashboardPage }))
const CandidatePage = lazy(async () => ({ default: (await import('./pages/CandidatePage')).CandidatePage }))
const CleaningPage = lazy(async () => ({ default: (await import('./pages/CleaningPage')).CleaningPage }))
const PublishedPage = lazy(async () => ({ default: (await import('./pages/PublishedPage')).PublishedPage }))
const AiReviewPage = lazy(async () => ({ default: (await import('./pages/AiReviewPage')).AiReviewPage }))
const RuleAgentPage = lazy(async () => ({ default: (await import('./pages/RuleAgentPage')).RuleAgentPage }))
const MetadataPage = lazy(async () => ({ default: (await import('./pages/MetadataPage')).MetadataPage }))
const UnifiedDevicePage = lazy(async () => ({ default: (await import('./pages/UnifiedDevicePage')).UnifiedDevicePage }))
const UnifiedLocationPage = lazy(async () => ({ default: (await import('./pages/UnifiedLocationPage')).UnifiedLocationPage }))
const KnowledgeAssetPage = lazy(async () => ({ default: (await import('./pages/KnowledgeAssetPage')).KnowledgeAssetPage }))
const SemanticFactPage = lazy(async () => ({ default: (await import('./pages/SemanticFactPage')).SemanticFactPage }))
const SemanticStatusDictionaryPage = lazy(async () => ({ default: (await import('./pages/SemanticStatusDictionaryPage')).SemanticStatusDictionaryPage }))
const SemanticEventPage = lazy(async () => ({ default: (await import('./pages/SemanticEventPage')).SemanticEventPage }))
const DecisionActionPage = lazy(async () => ({ default: (await import('./pages/DecisionActionPage')).DecisionActionPage }))
const OntologyRuntimePage = lazy(async () => ({ default: (await import('./pages/OntologyRuntimePage')).OntologyRuntimePage }))
const IdentityReviewPage = lazy(async () => ({ default: (await import('./pages/IdentityReviewPage')).IdentityReviewPage }))

export function App() {
  return <QueryClientProvider client={queryClient}>
    <ConfigProvider theme={{
      token: {
        colorPrimary: '#2563eb',
        colorBgLayout: '#f6f8fb',
        borderRadius: 10,
        fontFamily: 'system-ui, Microsoft YaHei, sans-serif',
      },
      components: {
        Layout: { headerBg: '#ffffff', siderBg: '#ffffff', bodyBg: '#f6f8fb' },
        Menu: { itemSelectedBg: '#eff6ff', itemSelectedColor: '#2563eb', itemBorderRadius: 8 },
        Card: { headerFontSize: 15 },
      },
    }}>
      <BrowserRouter>
        <AppLayout>
          <Suspense fallback={<div className="page-loading">正在加载页面…</div>}>
            <Routes>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/candidates" element={<CandidatePage />} />
              <Route path="/cleaning" element={<CleaningPage />} />
              <Route path="/ai-review" element={<AiReviewPage />} />
              <Route path="/rule-agent" element={<RuleAgentPage />} />
              <Route path="/metadata" element={<MetadataPage />} />
              <Route path="/unified-devices" element={<UnifiedDevicePage />} />
              <Route path="/unified-locations" element={<UnifiedLocationPage />} />
              <Route path="/identity-review" element={<IdentityReviewPage />} />
              <Route path="/ontology-runtime" element={<OntologyRuntimePage />} />
              <Route path="/knowledge-assets" element={<KnowledgeAssetPage />} />
              <Route path="/semantic-events" element={<SemanticEventPage />} />
              <Route path="/semantic-facts" element={<SemanticFactPage />} />
              <Route path="/semantic-status" element={<SemanticStatusDictionaryPage />} />
              <Route path="/decisions" element={<DecisionActionPage />} />
              <Route path="/published" element={<PublishedPage />} />
              <Route path="*" element={<DashboardPage />} />
            </Routes>
          </Suspense>
        </AppLayout>
      </BrowserRouter>
    </ConfigProvider>
  </QueryClientProvider>
}
