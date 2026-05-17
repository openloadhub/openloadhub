type EnvValue = string | boolean | undefined

const env = import.meta.env as Record<string, EnvValue>

const truthy = (value: EnvValue): boolean => {
  if (value === true) return true
  if (typeof value !== 'string') return false
  return ['1', 'true', 'yes', 'on'].includes(value.trim().toLowerCase())
}

const falsy = (value: EnvValue): boolean => {
  if (value === false) return true
  if (typeof value !== 'string') return false
  return ['0', 'false', 'no', 'off'].includes(value.trim().toLowerCase())
}

const publicAlphaMode = truthy(env.VITE_PTP_PUBLIC_ALPHA_MODE)

const featureEnabled = (key: string, defaultValue: boolean): boolean => {
  const value = env[key]
  if (truthy(value)) return true
  if (falsy(value)) return false
  return defaultValue
}

export const publicAlphaFeatures = {
  publicAlphaMode,
  themeSwitcher: featureEnabled('VITE_PTP_ENABLE_THEME_SWITCHER', true),
  mixedRuns: featureEnabled('VITE_PTP_ENABLE_MIXED_RUNS', !publicAlphaMode),
  selfApm: featureEnabled('VITE_PTP_ENABLE_SELF_APM', !publicAlphaMode),
  dynamicK6Control: featureEnabled('VITE_PTP_ENABLE_DYNAMIC_K6_CONTROL', !publicAlphaMode),
  aiFeatures: featureEnabled('VITE_PTP_ENABLE_AI_FEATURES', !publicAlphaMode),
  plans: featureEnabled('VITE_PTP_ENABLE_PLANS', true),
  planRuns: featureEnabled('VITE_PTP_ENABLE_PLAN_RUNS', true),
  trendAnalysis: featureEnabled('VITE_PTP_ENABLE_TREND_ANALYSIS', !publicAlphaMode),
}
