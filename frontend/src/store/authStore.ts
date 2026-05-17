import { create } from 'zustand'
import type { User } from '../types'

const normalizeRole = (role?: string | null): string => role?.trim().toUpperCase() ?? ''

export const deriveIsAdmin = (user?: User | null): boolean => {
  if (!user) {
    return false
  }
  return user.is_superuser === true || ['ADMIN', 'ADMINISTRATOR'].includes(normalizeRole(user.role))
}

interface AuthState {
  user: User | null
  token: string | null
  isAuthenticated: boolean
  isAdmin: boolean
  login: (token: string, user: User) => void
  logout: () => void
  updateUser: (user: Partial<User>) => void
  init: () => void
}

export const useAuthStore = create<AuthState>(set => ({
  user: null,
  token: null,
  isAuthenticated: false,
  isAdmin: false,
  login: (token, user) => {
    localStorage.setItem('token', token)
    localStorage.setItem('user', JSON.stringify(user))
    set({ token, user, isAuthenticated: true, isAdmin: deriveIsAdmin(user) })
  },
  logout: () => {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    set({ token: null, user: null, isAuthenticated: false, isAdmin: false })
  },
  updateUser: user =>
    set(state => {
      const updatedUser = state.user ? { ...state.user, ...user } : null
      if (updatedUser) {
        localStorage.setItem('user', JSON.stringify(updatedUser))
      }
      return { user: updatedUser, isAdmin: deriveIsAdmin(updatedUser) }
    }),
  init: () => {
    const token = localStorage.getItem('token')
    const userStr = localStorage.getItem('user')
    if (token && userStr) {
      try {
        const user = JSON.parse(userStr) as User
        set({ token, user, isAuthenticated: true, isAdmin: deriveIsAdmin(user) })
      } catch {
        // 解析失败，清除数据
        localStorage.removeItem('token')
        localStorage.removeItem('user')
      }
    }
  },
}))

// 初始化时从 localStorage 恢复状态
if (typeof window !== 'undefined') {
  useAuthStore.getState().init()
}
