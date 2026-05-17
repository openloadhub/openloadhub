interface UserLike {
  id?: number | string | null
}

const toPositiveInt = (value: unknown): number | undefined => {
  if (value === null || value === undefined || value === '') {
    return undefined
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

const parseJwtPayload = (token: string): Record<string, unknown> | null => {
  try {
    const payload = token.split('.')[1]
    if (!payload) {
      return null
    }
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
    return JSON.parse(window.atob(padded)) as Record<string, unknown>
  } catch {
    return null
  }
}

export const isJwtExpired = (token?: string | null, skewSeconds = 0): boolean => {
  if (!token) {
    return false
  }
  const exp = parseJwtPayload(token)?.exp
  if (typeof exp !== 'number') {
    return false
  }
  return exp <= Math.floor(Date.now() / 1000) + skewSeconds
}

export const resolveUserId = (token?: string | null, user?: UserLike | null): number | undefined => {
  const jwtSub = token ? toPositiveInt(parseJwtPayload(token)?.sub) : undefined
  if (jwtSub) {
    return jwtSub
  }
  return toPositiveInt(user?.id)
}
