import { Navigate, useLocation, useParams } from 'react-router-dom'

export const CompatibilityTaskDetailRedirect = () => {
  const { id } = useParams<{ id?: string }>()
  if (!id) return <Navigate to="/tasks" replace />
  return <Navigate to={`/tasks/${id}/edit`} replace />
}

export const CompatibilityPlanDetailRedirect = () => {
  const { planId } = useParams<{ planId?: string }>()
  if (!planId) return <Navigate to="/plans" replace />
  return <Navigate to={`/plans/${planId}/edit`} replace />
}

export const CompatibilityReportListRedirect = () => {
  const location = useLocation()
  return <Navigate to={`/my-focus/result-list${location.search}`} replace />
}
