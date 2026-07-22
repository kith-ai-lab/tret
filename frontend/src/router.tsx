import { Navigate, Route, Routes } from 'react-router-dom'

import { Approvals } from './views/Approvals'
import { Chat } from './views/Chat'
import { Documents } from './views/Documents'
import { Harnesses } from './views/Harnesses'
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
      <Route path="/documents" element={<Documents />} />
      <Route path="/packs" element={<Packs />} />
      <Route path="/harnesses" element={<Harnesses />} />
      <Route path="/settings" element={<SettingsView />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
