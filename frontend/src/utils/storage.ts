// 本地存储工具函数
export const storage = {
  get: <T>(key: string): T | null => {
    try {
      const item = localStorage.getItem(key)
      return item ? (JSON.parse(item) as T) : null
    } catch {
      return null
    }
  },

  set: <T>(key: string, value: T): void => {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch (error) {
      console.error('Storage set error:', error)
    }
  },

  remove: (key: string): void => {
    localStorage.removeItem(key)
  },

  clear: (): void => {
    localStorage.clear()
  },
}
