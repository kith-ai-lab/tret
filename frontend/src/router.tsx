import { Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'

import { isRouteHidden, useEdition } from './edition'

import { Analytics } from './views/Analytics'
import { Approvals } from './views/Approvals'
import { BatchCompare } from './views/BatchCompare'
import { BatchExperiment } from './views/BatchExperiment'
import { BatchFamily } from './views/BatchFamily'
import { SharedBatch } from './views/SharedBatch'
import { Batches } from './views/Batches'
import { Chat } from './views/Chat'
import { ConnectionsView } from './views/Connections'
import { Deliverables } from './views/Deliverables'
import { Documents } from './views/Documents'
import { Emissions } from './views/Emissions'
import { Evaluation } from './views/Evaluation'
import { Harnesses } from './views/Harnesses'
import { InviteAccept } from './views/InviteAccept'
import { MarketplaceReview } from './views/MarketplaceReview'
import { Packs } from './views/Packs'
import { RunDetailView } from './views/RunDetail'
import { Runs } from './views/Runs'
import { SettingsView } from './views/SettingsView'
import { Workbench } from './views/Workbench'

function CollectionRedirect() {
  const { id } = useParams<{ id: string }>()
  return <Navigate to={id ? `/batches/${id}` : '/batches'} replace />
}

export function AppRoutes() {
  const edition = useEdition()
  const { pathname } = useLocation()
  // A hidden route in this edition bounces home (standard hides nothing).
  if (isRouteHidden(edition, pathname)) return <Navigate to="/" replace />
  return (
    <Routes>
      <Route path="/" element={<Chat />} />
      <Route path="/chat" element={<Navigate to="/" replace />} />
      <Route path="/workbench" element={<Workbench />} />
      <Route path="/runs" element={<Runs />} />
      <Route path="/runs/:id" element={<RunDetailView />} />
      <Route path="/approvals" element={<Approvals />} />
      <Route path="/batches" element={<Batches />} />
      <Route path="/batches/compare" element={<BatchCompare />} />
      <Route path="/batches/family" element={<BatchFamily />} />
      <Route path="/batches/experiments/:id" element={<BatchExperiment />} />
      <Route path="/batches/shared/:id" element={<SharedBatch />} />
      <Route path="/batches/:id" element={<Batches />} />
      <Route path="/collections" element={<CollectionRedirect />} />
      <Route path="/collections/:id" element={<CollectionRedirect />} />
      <Route path="/analytics" element={<Analytics />} />
      <Route path="/emissions" element={<Emissions />} />
      <Route path="/deliverables" element={<Deliverables />} />
      <Route path="/documents" element={<Documents />} />
      <Route path="/packs" element={<Packs />} />
      {/* Cloud-only; the component itself probes and bounces home if the
          review API is not there (self-host, or an authenticated non-staff
          user) — see MarketplaceReview.tsx's own doc comment. */}
      <Route path="/marketplace-review" element={<MarketplaceReview />} />
      {/* Staff-only; the component probes the tournaments API (404 while the
          feature setting is off) and bounces home, like Marketplace review. */}
      <Route path="/evaluation" element={<Evaluation />} />
      <Route path="/harnesses" element={<Harnesses />} />
      <Route path="/settings" element={<SettingsView />} />
      <Route path="/settings/connections" element={<ConnectionsView />} />
      <Route path="/invite/:token" element={<InviteAccept />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
