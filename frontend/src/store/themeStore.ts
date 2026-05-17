import { create } from 'zustand'
import { publicAlphaFeatures } from '@/config/publicAlpha'

export type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'openloadhub-theme'
const DEFAULT_THEME: Theme = 'light'
const themeSwitcherEnabled = publicAlphaFeatures.themeSwitcher

const normalizeTheme = (value: string | null): Theme => (
    !themeSwitcherEnabled
        ? DEFAULT_THEME
        : value === 'dark' || value === 'light' ? value : DEFAULT_THEME
)

interface ThemeState {
    theme: Theme
    toggleTheme: () => void
    setTheme: (theme: Theme) => void
}

export const useThemeStore = create<ThemeState>((set) => ({
    theme: normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY)),
    toggleTheme: () =>
        set((state) => {
            if (!themeSwitcherEnabled) {
                localStorage.setItem(THEME_STORAGE_KEY, DEFAULT_THEME)
                updateDocumentTheme(DEFAULT_THEME)
                return { theme: DEFAULT_THEME }
            }
            const newTheme = state.theme === 'light' ? 'dark' : 'light'
            localStorage.setItem(THEME_STORAGE_KEY, newTheme)
            updateDocumentTheme(newTheme)
            return { theme: newTheme }
        }),
    setTheme: (theme) => {
        const nextTheme = themeSwitcherEnabled ? theme : DEFAULT_THEME
        localStorage.setItem(THEME_STORAGE_KEY, nextTheme)
        set(() => {
            updateDocumentTheme(nextTheme)
            return { theme: nextTheme }
        })
    },
}))

// Helper to sync with DOM
const updateDocumentTheme = (theme: Theme) => {
    document.documentElement.setAttribute('data-theme', `apple-${theme}`)
}

// Initialize
const initialTheme = normalizeTheme(localStorage.getItem(THEME_STORAGE_KEY))
updateDocumentTheme(initialTheme)
