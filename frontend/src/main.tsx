import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
// Self-hosted fonts: the product's network-isolation doctrine forbids a
// Google Fonts CDN link, so these ship as woff2 assets in the build instead.
import '@fontsource-variable/instrument-sans/wght.css'
import '@fontsource-variable/inter/wght.css'
import '@fontsource-variable/jetbrains-mono/wght.css'
import './theme/global.css'
import './components/chat/delegated-work.css'
// Hosted-build brand layer. Every rule is scoped to <html data-brand="kith-climate">,
// so it is inert in the open-source build.
import './theme/brand-kith-climate.css'
import { BRAND } from './brand'

if (BRAND === 'kith-climate') {
  document.querySelector('link[rel="icon"]')?.setAttribute('href', '/brands/kith-climate/favicon.svg')
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 15_000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
