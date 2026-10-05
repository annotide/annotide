import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { Route, Routes } from 'react-router-dom'
import i18n from '@/i18n'
import { Layout } from '@/components/Layout'
import { Button } from '@/components/Button'
import { ProjectListPage } from '@/pages/ProjectListPage'
import { ProjectDetailPage } from '@/pages/ProjectDetailPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { ProjectSettingsPage } from '@/pages/ProjectSettingsPage'
import { ConnectorsPage } from '@/pages/ConnectorsPage'
import { ModelsPage } from '@/pages/ModelsPage'
import { WebhooksPage } from '@/pages/WebhooksPage'
import { ApiKeysPage } from '@/pages/ApiKeysPage'
import { LicensePage } from '@/pages/LicensePage'
import { SecurityPage } from '@/pages/SecurityPage'
import { UsersPage } from '@/pages/UsersPage'
import { AnnotatePage } from '@/pages/AnnotatePage'
import { ReviewPage } from '@/pages/ReviewPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { LoginPage } from '@/pages/LoginPage'
import { AuthCallbackPage } from '@/pages/AuthCallbackPage'
import { RequireAuth } from '@/components/RequireAuth'
import { useSessionCheck } from '@/lib/useSession'
import { useThemeClass } from '@/lib/useTheme'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * Top-level error boundary. Implemented as a class component because React
 * has no hook-based equivalent for catching render errors — this is the one
 * necessary exception to the "no class components" rule.
 */
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error('Unhandled application error', error, info)
  }

  render(): ReactNode {
    const { error } = this.state
    if (error) {
      return (
        <div
          role="alert"
          aria-live="assertive"
          className="flex min-h-screen flex-col items-center justify-center gap-4 bg-surface p-8 text-center text-ink"
        >
          <p className="text-lg font-semibold">{i18n.t('somethingWentWrong')}</p>
          <p className="max-w-prose text-sm text-muted">{error.message}</p>
          <Button variant="secondary" onClick={() => this.setState({ error: null })}>
            {i18n.t('tryAgain')}
          </Button>
        </div>
      )
    }
    return this.props.children
  }
}

export default function App(): JSX.Element {
  // Applies the stored light/dark preference to <html> (UX-7).
  useThemeClass()
  // Drops a restored session whose account no longer exists on the server.
  useSessionCheck()

  return (
    <ErrorBoundary>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/auth/callback" element={<AuthCallbackPage />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/" element={<ProjectListPage />} />
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          <Route path="/projects/:projectId/dashboard" element={<DashboardPage />} />
          <Route path="/projects/:projectId/settings" element={<ProjectSettingsPage />} />
          <Route path="/connectors" element={<ConnectorsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/webhooks" element={<WebhooksPage />} />
          <Route path="/settings/api-keys" element={<ApiKeysPage />} />
          <Route path="/settings/license" element={<LicensePage />} />
          <Route path="/settings/security" element={<SecurityPage />} />
          <Route path="/settings/users" element={<UsersPage />} />
          <Route path="/projects/:projectId/annotate/:itemId?" element={<AnnotatePage />} />
          <Route path="/projects/:projectId/review/:itemId?" element={<ReviewPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </ErrorBoundary>
  )
}
