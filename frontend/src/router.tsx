import { Navigate, Route, Routes } from 'react-router-dom'

import { Analytics } from './views/Analytics'
import { Approvals } from './views/Approvals'
import { Chat } from './views/Chat'
import { Deliverables } from './views/Deliverables'
import { Documents } from './views/Documents'
import { Emissions } from './views/Emissions'
import { Harnesses } from './views/Harnesses'
import { InviteAccept } from './views/InviteAccept'
import { MarketplaceReview } from './views/MarketplaceReview'
import { Packs } from './views/Packs'
import { RunDetailView } from './views/RunDetail'
import { Runs } from './views/Runs'
import { SettingsView } from './views/SettingsView'
import { Workbench } from './views/Workbench'

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Chat />} />
      <Route path="/chat" element={<Navigate to="/" replace />} />
      <Route path="/workbench" element={<Workbench />} />
      <Route path="/runs" element={<Runs />} />
      <Route path="/runs/:id" element={<RunDetailView />} />
      <Route path="/approvals" element={<Approvals />} />
      <Route path="/analytics" element={<Analytics />} />
      <Route path="/emissions" element={<Emissions />} />
      <Route path="/deliverables" element={<Deliverables />} />
      <Route path="/documents" element={<Documents />} />
      <Route path="/packs" element={<Packs />} />
      {/* Cloud-only; the component itself probes and bounces home if the
          review API is not there (self-host, or an authenticated non-staff
          user) — see MarketplaceReview.tsx's own doc comment. */}
      <Route path="/marketplace-review" element={<MarketplaceReview />} />
      <Route path="/harnesses" element={<Harnesses />} />
      <Route path="/settings" element={<SettingsView />} />
      <Route path="/invite/:token" element={<InviteAccept />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
