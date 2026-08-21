import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AppLayout } from './layouts/AppLayout'
import { DashboardPage } from './pages/DashboardPage'
import { CandidatePage } from './pages/CandidatePage'
import { CleaningPage } from './pages/CleaningPage'
import { PublishedPage } from './pages/PublishedPage'
import { AiReviewPage } from './pages/AiReviewPage'
import { RuleAgentPage } from './pages/RuleAgentPage'
import { MetadataPage } from './pages/MetadataPage'
import { UnifiedDevicePage } from './pages/UnifiedDevicePage'
import { UnifiedLocationPage } from './pages/UnifiedLocationPage'
import { KnowledgeAssetPage } from './pages/KnowledgeAssetPage'
import { SemanticFactPage } from './pages/SemanticFactPage'
import { SemanticStatusDictionaryPage } from './pages/SemanticStatusDictionaryPage'
import { SemanticEventPage } from './pages/SemanticEventPage'
import { DecisionActionPage } from './pages/DecisionActionPage'
import { OntologyRuntimePage } from './pages/OntologyRuntimePage'
import { IdentityReviewPage } from './pages/IdentityReviewPage'
import './styles/app.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 0 } } })

export function App() {
  return <QueryClientProvider client={queryClient}><ConfigProvider theme={{ token: { colorPrimary: '#2563eb', colorBgLayout: '#f6f8fb', borderRadius: 10, fontFamily: 'system-ui, Microsoft YaHei, sans-serif' }, components: { Layout: { headerBg: '#ffffff', siderBg: '#ffffff', bodyBg: '#f6f8fb' }, Menu: { itemSelectedBg: '#eff6ff', itemSelectedColor: '#2563eb', itemBorderRadius: 8 }, Card: { headerFontSize: 15 } } }}><BrowserRouter><AppLayout><Routes><Route path="/" element={<DashboardPage />} /><Route path="/candidates" element={<CandidatePage />} /><Route path="/cleaning" element={<CleaningPage />} /><Route path="/ai-review" element={<AiReviewPage />} /><Route path="/rule-agent" element={<RuleAgentPage />} /><Route path="/metadata" element={<MetadataPage />} /><Route path="/unified-devices" element={<UnifiedDevicePage />} /><Route path="/unified-locations" element={<UnifiedLocationPage />} /><Route path="/identity-review" element={<IdentityReviewPage />} /><Route path="/ontology-runtime" element={<OntologyRuntimePage />} /><Route path="/knowledge-assets" element={<KnowledgeAssetPage />} /><Route path="/semantic-events" element={<SemanticEventPage />} /><Route path="/semantic-facts" element={<SemanticFactPage />} /><Route path="/semantic-status" element={<SemanticStatusDictionaryPage />} /><Route path="/decisions" element={<DecisionActionPage />} /><Route path="/published" element={<PublishedPage />} /><Route path="*" element={<DashboardPage />} /></Routes></AppLayout></BrowserRouter></ConfigProvider></QueryClientProvider>
}
