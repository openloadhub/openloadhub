import axios, { AxiosError, AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios'
import { message } from 'antd'
import type { ApiResponse } from '../types'
import { isJwtExpired, resolveUserId } from './auth'

let authExpiredNotified = false

const buildLoginUrl = (): string => {
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`
  const params = new URLSearchParams()
  if (current && current !== '/login') {
    params.set('redirect_to', current)
  }
  const query = params.toString()
  return query ? `/login?${query}` : '/login'
}

const redirectToLogin = (notice = '登录已过期，请重新登录') => {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
  if (!authExpiredNotified) {
    authExpiredNotified = true
    message.error(notice)
  }
  if (window.location.pathname !== '/login') {
    window.location.href = buildLoginUrl()
  }
}

// 创建 axios 实例
const axiosInstance: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器 - 添加 JWT Token
axiosInstance.interceptors.request.use(
  config => {
    const token = localStorage.getItem('token')
    if (token) {
      if (isJwtExpired(token)) {
        redirectToLogin()
        return Promise.reject(new Error('登录已过期，请重新登录'))
      }
      config.headers.Authorization = `Bearer ${token}`
    }

    // 过渡期 前端鉴权透传（后续替换为后端 JWT claims 解析）
    const userRaw = localStorage.getItem('user')
    if (userRaw) {
      try {
        const parsed = JSON.parse(userRaw) as { id?: number | string; role?: string; is_superuser?: boolean }
        const userId = resolveUserId(token, parsed)
        if (userId) {
          config.headers['X-User-Id'] = String(userId)
        }
        if (typeof parsed.role === 'string' && parsed.role) {
          config.headers['X-User-Role'] = parsed.role
        }
        if (typeof parsed.is_superuser === 'boolean') {
          config.headers['X-Is-Superuser'] = parsed.is_superuser ? '1' : '0'
        }
      } catch {
        // ignore parse error
      }
    } else {
      const userId = resolveUserId(token)
      if (userId) {
        config.headers['X-User-Id'] = String(userId)
      }
    }
    return config
  },
  error => {
    return Promise.reject(error)
  }
)

// 响应拦截器 - 统一处理错误
axiosInstance.interceptors.response.use(
  (response: AxiosResponse<ApiResponse<unknown>>) => {
    const config = response.config as RequestConfig

    // 二进制下载保持原始响应
    if (config.responseType === 'blob' || config.responseType === 'arraybuffer') {
      return response as AxiosResponse<unknown>
    }

    const payload = response.data
    if (config.skipErrorHandler) {
      if (payload && typeof payload === 'object' && 'code' in payload && 'data' in payload) {
        const apiPayload = payload as ApiResponse<unknown>
        if (apiPayload.code !== 0) {
          return Promise.reject(new Error(apiPayload.message || '请求失败'))
        }
        return {
          ...response,
          data: apiPayload.data as unknown,
        } as AxiosResponse<unknown>
      }
      return response as AxiosResponse<unknown>
    }

    if (!payload || typeof payload !== 'object') {
      message.error('响应格式错误')
      return Promise.reject(new Error('响应格式错误'))
    }

    const { code, message: msg, data } = payload

    // 约定：code === 0 表示成功，其余为业务错误
    if (code !== 0) {
      if (code === 401 || code === 401001) {
        redirectToLogin(msg || '登录已过期，请重新登录')
        return Promise.reject(new Error(msg || '登录已过期，请重新登录'))
      }
      message.error(msg || '请求失败')
      return Promise.reject(new Error(msg || '请求失败'))
    }

    return {
      ...response,
      data: data as unknown,
    } as AxiosResponse<unknown>
  },
  (error: AxiosError<ApiResponse>) => {
    const requestConfig = error.config as RequestConfig | undefined
    if (requestConfig?.skipErrorHandler) {
      return Promise.reject(error)
    }

    const { response } = error
    if (response) {
      const { status, data } = response
      const errorDetail = typeof (data as { detail?: unknown } | undefined)?.detail === 'string'
        ? (data as { detail?: string }).detail
        : undefined

      switch (status) {
        case 401:
          redirectToLogin(data?.message || errorDetail || '登录已过期，请重新登录')
          break
        case 403:
          message.error('没有权限访问该资源')
          break
        case 404:
          message.error('请求的资源不存在')
          break
        case 500:
          message.error(data?.message || errorDetail || '服务器内部错误')
          break
        default:
          message.error(data?.message || errorDetail || `请求失败 (${status})`)
      }
    } else if (error.request) {
      message.error('网络错误，请检查网络连接')
    } else {
      message.error('请求配置错误')
    }

    return Promise.reject(error)
  }
)

// 自定义请求方法，返回类型统一为 T（即业务 data）
interface RequestConfig extends AxiosRequestConfig {
  skipErrorHandler?: boolean
}

const request = {
  get: <T = unknown>(url: string, config?: RequestConfig): Promise<T> => {
    return axiosInstance.get<ApiResponse<T>>(url, config).then(res => (res as AxiosResponse<T>).data)
  },
  post: <T = unknown>(url: string, data?: unknown, config?: RequestConfig): Promise<T> => {
    return axiosInstance.post<ApiResponse<T>>(url, data, config).then(res => (res as AxiosResponse<T>).data)
  },
  put: <T = unknown>(url: string, data?: unknown, config?: RequestConfig): Promise<T> => {
    return axiosInstance.put<ApiResponse<T>>(url, data, config).then(res => (res as AxiosResponse<T>).data)
  },
  delete: <T = unknown>(url: string, config?: RequestConfig): Promise<T> => {
    return axiosInstance.delete<ApiResponse<T>>(url, config).then(res => (res as AxiosResponse<T>).data)
  },
  patch: <T = unknown>(url: string, data?: unknown, config?: RequestConfig): Promise<T> => {
    return axiosInstance.patch<ApiResponse<T>>(url, data, config).then(res => (res as AxiosResponse<T>).data)
  },
}

export default request
