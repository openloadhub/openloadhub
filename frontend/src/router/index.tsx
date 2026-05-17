import { Button, Result } from 'antd'
import { createBrowserRouter, Navigate, useNavigate } from 'react-router-dom'
import Layout from '../components/Layout'
import Login from '../pages/Login'
import Watchlist from '../pages/Watchlist'
import TaskList from '../pages/TaskList'
import TaskCreate from '../pages/TaskCreate'
import ScriptList from '../pages/ScriptList'
import ReportList from '../pages/ReportList'
import ReportDetail from '../pages/ReportDetail'
import ReportHtmlViewer from '../pages/ReportHtmlViewer'
import AgentList from '../pages/AgentList'
import RunList from '../pages/RunList'
import RunDetail from '../pages/RunDetail'
import RunCompare from '../pages/RunCompare'
import PlanList from '../pages/PlanList'
import PlanCreateEdit from '../pages/PlanCreateEdit'
import PlanRunList from '../pages/PlanRunList'
import PlanRunDetail from '../pages/PlanRunDetail'
import TaskDetail from '../pages/TaskDetail'
import K6Guide from '../pages/K6Guide'
import ProtectedRoute from '../components/ProtectedRoute'
import { CompatibilityPlanDetailRedirect, CompatibilityReportListRedirect, CompatibilityTaskDetailRedirect } from './compatibilityRedirects'
import { publicAlphaFeatures } from '@/config/publicAlpha'

interface PublicAlphaRoadmapPageProps {
  featureName: string
}

const PublicAlphaRoadmapPage = ({ featureName }: PublicAlphaRoadmapPageProps) => {
  const navigate = useNavigate()

  return (
    <Result
      status="info"
      title={`${featureName} 暂未开放`}
      subTitle={`${featureName}已进入 OpenLoadHub roadmap；public v0.1 alpha 暂不提供可操作功能。`}
      extra={
        <Button type="primary" onClick={() => navigate('/my-focus/focus-task')}>
          返回关注任务
        </Button>
      }
    />
  )
}

export const router = createBrowserRouter([
  {
    path: '/login',
    element: <Login />,
  },
  {
    path: '/',
    element: (
      <ProtectedRoute>
        <Layout />
      </ProtectedRoute>
    ),
    children: [
      {
        index: true,
        element: <Navigate to="/my-focus/focus-task" replace />,
      },
      {
        path: 'my-focus/focus-task',
        element: <Watchlist />,
      },
      {
        path: 'my-focus/focus-task/create-http-task',
        element: <Navigate to="/tasks/new?type=http" replace />,
      },
      {
        path: 'dashboard',
        element: <Navigate to="/my-focus/focus-task" replace />,
      },
      {
        path: 'watchlist',
        element: <Navigate to="/my-focus/focus-task" replace />,
      },
      {
        path: 'tasks',
        element: <TaskList />,
      },
      {
        path: 'guides/k6-standard-scripts',
        element: <K6Guide />,
      },
      {
        path: 'tasks/new',
        element: <TaskCreate />,
      },
      {
        path: 'tasks/create',
        element: <Navigate to="/tasks/new" replace />,
      },
      {
        path: 'tasks/:taskId/edit',
        element: <TaskCreate />,
      },
      {
        path: 'tasks/:taskId',
        element: <TaskDetail />,
      },
      {
        path: 'tasks/:id',
        element: <CompatibilityTaskDetailRedirect />,
      },
      {
        path: 'scripts',
        element: <ScriptList />,
      },
      {
        path: 'runs',
        element: <RunList />,
      },
      {
        path: 'runs/compare',
        element: <RunCompare />,
      },
      {
        path: 'runs/:runId',
        element: <RunDetail />,
      },
      {
        path: 'plans',
        element: publicAlphaFeatures.plans ? <PlanList /> : <Navigate to="/runs" replace />,
      },
      {
        path: 'plans/new',
        element: publicAlphaFeatures.plans ? <PlanCreateEdit /> : <Navigate to="/runs" replace />,
      },
      {
        path: 'plans/:planId/edit',
        element: publicAlphaFeatures.plans ? <PlanCreateEdit /> : <Navigate to="/runs" replace />,
      },
      {
        path: 'plans/:planId',
        element: publicAlphaFeatures.plans ? <CompatibilityPlanDetailRedirect /> : <Navigate to="/runs" replace />,
      },
      {
        path: 'plan-runs',
        element: publicAlphaFeatures.planRuns ? <PlanRunList /> : <Navigate to="/runs" replace />,
      },
      {
        path: 'plan-runs/:planRunId',
        element: publicAlphaFeatures.planRuns ? <PlanRunDetail /> : <Navigate to="/runs" replace />,
      },
      {
        path: 'mixed-runs',
        element: <PublicAlphaRoadmapPage featureName="混压执行" />,
      },
      {
        path: 'mixed-runs/new',
        element: <PublicAlphaRoadmapPage featureName="混压执行" />,
      },
      {
        path: 'mixed-runs/:mixedRunId',
        element: <PublicAlphaRoadmapPage featureName="混压执行" />,
      },
      {
        path: 'mixed-runs/:mixedRunId/reports/:reportId/view',
        element: <PublicAlphaRoadmapPage featureName="混压执行" />,
      },
      {
        path: 'my-focus/result-list',
        element: <ReportList />,
      },
      {
        path: 'reports',
        element: <CompatibilityReportListRedirect />,
      },
      {
        path: 'reports/:id',
        element: <ReportDetail />,
      },
      {
        path: 'reports/:id/view',
        element: <ReportHtmlViewer kind="run" />,
      },
      {
        path: 'trend-analysis',
        element: <PublicAlphaRoadmapPage featureName="趋势分析" />,
      },
      {
        path: 'analysis',
        element: <PublicAlphaRoadmapPage featureName="趋势分析" />,
      },
      {
        path: 'agents',
        element: (
          <ProtectedRoute requireAdmin>
            <AgentList />
          </ProtectedRoute>
        ),
      },
      {
        path: 'self-apm',
        element: (
          <ProtectedRoute requireAdmin>
            <PublicAlphaRoadmapPage featureName="Self-APM" />
          </ProtectedRoute>
        ),
      },
    ],
  },
])
