import { Navigate, useLocation } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'

interface ProtectedRouteProps {
  children: React.ReactNode
  requireAdmin?: boolean
  fallbackPath?: string
}

const ProtectedRoute = ({ children, requireAdmin = false, fallbackPath = '/my-focus/focus-task' }: ProtectedRouteProps) => {
  const { isAuthenticated, isAdmin } = useAuthStore()
  const location = useLocation()

  if (!isAuthenticated) {
    const redirectTo = `${location.pathname}${location.search}${location.hash}`
    return <Navigate to={`/login?redirect_to=${encodeURIComponent(redirectTo)}`} replace />
  }

  if (requireAdmin && !isAdmin) {
    return <Navigate to={fallbackPath} replace />
  }

  return <>{children}</>
}

export default ProtectedRoute
