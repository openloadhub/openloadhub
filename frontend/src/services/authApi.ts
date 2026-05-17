import request from '../utils/request'
import type { LoginRequest, LoginResponse, User } from '../types'

export const authApi = {
  // 登录
  login: (data: LoginRequest): Promise<LoginResponse> => {
    // 登录接口返回 Token 结构而非统一 ApiResponse
    // 因此这里通过 skipErrorHandler 跳过通用 code 校验，直接返回原始数据
    return request.post('/auth/login', data, { skipErrorHandler: true })
  },

  // 登出
  logout: (): Promise<{ message: string }> => {
    return request.post('/auth/logout', {}, { skipErrorHandler: true })
  },

  // 获取当前用户信息
  getCurrentUser: (): Promise<User> => {
    return request.get('/auth/me', { skipErrorHandler: true })
  },

  // 刷新 token
  refreshToken: (): Promise<LoginResponse> => {
    return request.post('/auth/refresh', {}, { skipErrorHandler: true })
  },
}
