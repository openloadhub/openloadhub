import { Form, Input, Button, Card, message } from 'antd'
import { ApiOutlined, ClusterOutlined, LockOutlined, SafetyCertificateOutlined, UserOutlined } from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { authApi } from '../../services/authApi'
import { useAuthStore } from '../../store/authStore'
import type { LoginRequest } from '../../types'
import { publicAlphaFeatures } from '../../config/publicAlpha'

const Login = () => {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { login } = useAuthStore()
  const isPublicAlpha = publicAlphaFeatures.publicAlphaMode
  const title = isPublicAlpha ? 'OpenLoadHub' : 'OpenLoadHub'
  const subtitle = isPublicAlpha ? '开源压测控制面' : 'OpenLoadHub'

  const { mutate, isPending } = useMutation({
    mutationFn: (data: LoginRequest) => authApi.login(data),
    onSuccess: data => {
      // 后端返回 access_token，这里统一映射到本地 token
      login(data.access_token, data.user)
      message.success('登录成功')
      const redirectTo = searchParams.get('redirect_to') || '/my-focus/focus-task'
      navigate(redirectTo.startsWith('/') ? redirectTo : '/my-focus/focus-task', { replace: true })
    },
    onError: (error: unknown) => {
      const errorMessage = error instanceof Error ? error.message : '登录失败，请检查用户名和密码'
      message.error(errorMessage)
    },
  })

  const onFinish = (values: LoginRequest) => {
    mutate(values)
  }

  return (
    <div className="olh-login-page">
      <section className="olh-login-shell" aria-label="OpenLoadHub 登录">
        <div className="olh-login-intro">
          <div className="olh-login-brandmark">
            {isPublicAlpha ? (
              <>
                <img
                  className="olh-login-brandmark-image"
                  src="/brand/openloadhub-mark.svg"
                  alt=""
                  width={22}
                  height={22}
                  aria-hidden="true"
                  onError={event => {
                    event.currentTarget.style.display = 'none'
                    event.currentTarget.nextElementSibling?.removeAttribute('hidden')
                  }}
                />
                <span className="olh-login-brandmark-dot" hidden />
              </>
            ) : (
              <span className="olh-login-brandmark-dot" />
            )}
            <span>OpenLoadHub Console</span>
          </div>
          <h1>{title}</h1>
          <p>{subtitle}，面向任务、批次、运行结果和 Agent 资源的开源压测工作台。</p>
          <div className="olh-login-signal-grid" aria-label="运行态概览">
            <div>
              <ApiOutlined />
              <span>API Gateway</span>
              <strong>ready</strong>
            </div>
            <div>
              <ClusterOutlined />
              <span>Agent Pool</span>
              <strong>runtime</strong>
            </div>
            <div>
              <SafetyCertificateOutlined />
              <span>Public Alpha</span>
              <strong>ops</strong>
            </div>
          </div>
        </div>

        <Card className="olh-login-card">
          <div className="olh-login-brand">
            <div className={`olh-login-mark ${isPublicAlpha ? 'olh-login-mark--public' : ''}`} aria-hidden="true">
              {isPublicAlpha ? (
                <>
                  <img
                    className="olh-login-mark-image"
                    src="/brand/openloadhub-mark.svg"
                    alt=""
                    width={40}
                    height={40}
                    aria-hidden="true"
                    onError={event => {
                      event.currentTarget.style.display = 'none'
                      event.currentTarget.nextElementSibling?.removeAttribute('hidden')
                    }}
                  />
                  <span hidden />
                </>
              ) : (
                <span />
              )}
            </div>
            <h2>{title}</h2>
            <p>登录后进入关注任务工作台</p>
            {isPublicAlpha ? <div className="olh-login-badge">PUBLIC ALPHA / OPERATIONS</div> : null}
          </div>

          <Form className="olh-login-form" name="login" onFinish={onFinish} autoComplete="off" size="large">
            <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
              <Input prefix={<UserOutlined />} placeholder="用户名" />
            </Form.Item>

            <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password prefix={<LockOutlined />} placeholder="密码" />
            </Form.Item>

            <Form.Item>
              <Button type="primary" htmlType="submit" block loading={isPending}>
                登录
              </Button>
            </Form.Item>
          </Form>
        </Card>
      </section>
    </div>
  )
}

export default Login
