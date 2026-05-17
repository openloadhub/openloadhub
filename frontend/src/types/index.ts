// API 响应类型
export interface ApiResponse<T = unknown> {
  code: number
  message: string
  data: T
}

// 用户信息
export interface User {
  id: number
  username: string
  email?: string
  role?: string
  is_superuser?: boolean
}

// 认证相关
export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  // 与后端 Token 模型保持一致
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

// 分页参数
export interface PaginationParams {
  page: number
  pageSize: number
}

export interface PaginationResponse<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
}
