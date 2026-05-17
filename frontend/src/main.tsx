import React from 'react'
import ReactDOM from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { publicAlphaFeatures } from './config/publicAlpha'
import { AppConfigProvider } from './providers/AppConfigProvider'
import { QueryProvider } from './providers/QueryProvider'
import { router } from './router'
import './styles/theme.css'
import './index.css'

document.title = publicAlphaFeatures.publicAlphaMode ? 'OpenLoadHub' : 'OpenLoadHub'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppConfigProvider>
      <QueryProvider>
        <RouterProvider router={router} />
      </QueryProvider>
    </AppConfigProvider>
  </React.StrictMode>
)
