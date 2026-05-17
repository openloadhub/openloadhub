import { Layout as AntLayout, Menu, Avatar, Dropdown, Modal, Space } from 'antd'
import { useNavigate, useLocation, Outlet } from 'react-router-dom'
import {
  StarOutlined,
  BarChartOutlined,
  LineChartOutlined,
  ScheduleOutlined,
  UserOutlined,
  LogoutOutlined,
  DashboardOutlined,
  DeploymentUnitOutlined,
} from '@ant-design/icons'
import { useAuthStore } from '../../store/authStore'
import { getCanonicalUserLabel } from '@/utils/displayUser'
import { publicAlphaFeatures } from '@/config/publicAlpha'
import type { MenuProps } from 'antd'

const { Header, Sider, Content } = AntLayout
const ROADMAP_KEY_PREFIX = 'public-alpha-roadmap:'

const roadmapNotices: Record<string, { title: string; description: string }> = {
  [`${ROADMAP_KEY_PREFIX}mixed-runs`]: {
    title: '混压执行暂未开放',
    description: '混压执行已进入 OpenLoadHub roadmap；public v0.1 alpha 暂不提供可操作功能。',
  },
  [`${ROADMAP_KEY_PREFIX}trend-analysis`]: {
    title: '趋势分析暂未开放',
    description: '趋势分析已进入 OpenLoadHub roadmap；public v0.1 alpha 暂不提供可操作功能。',
  },
  [`${ROADMAP_KEY_PREFIX}self-apm`]: {
    title: 'Self-APM 暂未开放',
    description: 'Self-APM 已进入 OpenLoadHub roadmap；public v0.1 alpha 暂不提供可操作功能。',
  },
}

const Layout = () => {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout, isAdmin } = useAuthStore()

  const menuItems: MenuProps['items'] = [
    {
      key: '/my-focus/focus-task',
      icon: <StarOutlined />,
      label: '关注任务',
    },
    {
      key: '/runs',
      icon: <BarChartOutlined />,
      label: '结果列表',
    },
    ...(publicAlphaFeatures.plans
      ? [
          {
            key: '/plans',
            icon: <ScheduleOutlined />,
            label: '批次模板',
          },
        ]
      : []),
    ...(publicAlphaFeatures.planRuns
      ? [
          {
            key: '/plan-runs',
            icon: <ScheduleOutlined />,
            label: '批次记录',
          },
        ]
      : []),
    ...(isAdmin
      ? [
          {
            key: '/agents',
            icon: <DeploymentUnitOutlined />,
            label: 'Agent 管理',
          },
        ]
      : []),
    {
      key: `${ROADMAP_KEY_PREFIX}mixed-runs`,
      icon: <ScheduleOutlined />,
      label: '混压执行',
    },
    {
      key: `${ROADMAP_KEY_PREFIX}trend-analysis`,
      icon: <LineChartOutlined />,
      label: '趋势分析',
    },
    {
      key: `${ROADMAP_KEY_PREFIX}self-apm`,
      icon: <DashboardOutlined />,
      label: 'Self-APM',
    },
  ]

  const userMenuItems: MenuProps['items'] = [
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      danger: true,
    },
  ]

  const handleMenuClick = ({ key }: { key: string }) => {
    const roadmapNotice = roadmapNotices[key]
    if (roadmapNotice) {
      Modal.info({
        title: roadmapNotice.title,
        content: roadmapNotice.description,
        okText: '知道了',
      })
      return
    }
    navigate(key)
  }

  const handleUserMenuClick: MenuProps['onClick'] = ({ key }) => {
    if (key === 'logout') {
      logout()
      navigate('/login')
    }
  }

  const getSelectedKeys = (): string[] => {
    const pathname = location.pathname
    const keys = menuItems
      .map(item => item?.key)
      .filter((key): key is string => typeof key === 'string')
      .sort((a, b) => b.length - a.length)

    for (const key of keys) {
      if (pathname === key) {
        return [key]
      }
      if (pathname.startsWith(`${key}/`)) {
        return [key]
      }
    }
    return []
  }

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        width={200}
        style={{
          overflow: 'auto',
          height: '100vh',
          position: 'fixed',
          left: 0,
          top: 0,
          bottom: 0,
        }}
      >
        <div
          style={{
            height: 64,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 18,
            fontWeight: 'bold',
            color: '#1890ff',
          }}
        >
          OpenLoadHub
        </div>
        <Menu
          mode="inline"
          selectedKeys={getSelectedKeys()}
          items={menuItems}
          onClick={handleMenuClick}
          style={{ borderRight: 0 }}
        />
      </Sider>
      <AntLayout style={{ marginLeft: 200 }}>
        <Header
          style={{
            padding: '0 24px',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            height: 64,
          }}
        >
          <div style={{ fontSize: 16, fontWeight: 500 }}></div>

          <Space size="large">
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                cursor: 'pointer',
                color: '#1890ff',
              }}
              onClick={() => navigate('/my-focus/focus-task')}
            >
              <StarOutlined style={{ marginRight: 8 }} />
              我的关注
            </div>

            <Dropdown
              menu={{ items: userMenuItems, onClick: handleUserMenuClick }}
              placement="bottomRight"
            >
              <Space style={{ cursor: 'pointer' }}>
                <Avatar icon={<UserOutlined />} />
                <span>{getCanonicalUserLabel(user?.username || '用户')}</span>
              </Space>
            </Dropdown>
          </Space>
        </Header>
        <Content
          style={{
            margin: '24px 16px',
            padding: 24,
            minHeight: 280,
          }}
        >
          <Outlet />
        </Content>
      </AntLayout>
    </AntLayout>
  )
}

export default Layout
