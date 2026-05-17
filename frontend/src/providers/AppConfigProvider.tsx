import React, { useEffect, useMemo } from 'react'
import { ConfigProvider, theme as antTheme, type ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useThemeStore } from '../store/themeStore'

interface AppConfigProviderProps {
    children: React.ReactNode
}

const getStyleTokens = (isDark: boolean): ThemeConfig['token'] => {
    const common = {
        fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        borderRadius: 8,
        borderRadiusLG: 8,
        borderRadiusSM: 6,
        wireframe: false,
    }

    if (isDark) {
        return {
            ...common,
            colorPrimary: '#14B8A6',
            colorInfo: '#14B8A6',
            colorSuccess: '#22C55E',
            colorWarning: '#F59E0B',
            colorError: '#FF5C6C',
            colorBgBase: '#101A27',
            colorBgLayout: '#101A27',
            colorBgContainer: '#172332',
            colorBgElevated: '#1C2A3B',
            colorBgSpotlight: '#223047',
            colorBorder: '#344358',
            colorBorderSecondary: '#2A394D',
            colorSplit: '#2A394D',
            colorText: '#D4DDE8',
            colorTextSecondary: '#A3B1C1',
            colorTextTertiary: '#8291A3',
            colorFillSecondary: 'rgba(148, 163, 184, 0.115)',
            colorFillTertiary: 'rgba(148, 163, 184, 0.078)',
            colorFillQuaternary: 'rgba(148, 163, 184, 0.048)',
            controlItemBgHover: 'rgba(148, 163, 184, 0.095)',
            controlItemBgActive: 'rgba(20, 184, 166, 0.125)',
        }
    }

    return {
        ...common,
        colorPrimary: '#0D9488',
        colorInfo: '#0D9488',
        colorSuccess: '#16A34A',
        colorWarning: '#B7791F',
        colorError: '#D92D20',
        colorBgBase: '#F7F8FA',
        colorBgLayout: '#F7F8FA',
        colorBgContainer: '#FFFFFF',
        colorBgElevated: '#FFFFFF',
        colorBgSpotlight: '#F4F6F8',
        colorBorder: '#D8E1EA',
        colorBorderSecondary: '#E6ECF2',
        colorSplit: '#E6ECF2',
        colorText: '#0F172A',
        colorTextSecondary: '#526173',
        colorTextTertiary: '#768599',
        colorFillSecondary: 'rgba(15, 23, 42, 0.055)',
        colorFillTertiary: 'rgba(15, 23, 42, 0.035)',
        colorFillQuaternary: 'rgba(15, 23, 42, 0.02)',
        controlItemBgHover: 'rgba(15, 23, 42, 0.045)',
        controlItemBgActive: 'rgba(13, 148, 136, 0.09)',
    }
}

const getComponentOverrides = (isDark: boolean): ThemeConfig['components'] => {
    const palette = isDark
        ? {
            bg: '#101A27',
            nav: '#121D2C',
            surface: '#172332',
            elevated: '#1C2A3B',
            border: '#344358',
            text: '#D4DDE8',
            secondary: '#A3B1C1',
            primary: '#14B8A6',
            primarySoft: 'rgba(20, 184, 166, 0.115)',
            hover: 'rgba(148, 163, 184, 0.095)',
        }
        : {
            bg: '#F7F8FA',
            nav: '#FFFFFF',
            surface: '#FFFFFF',
            elevated: '#FFFFFF',
            border: '#D8E1EA',
            text: '#0F172A',
            secondary: '#526173',
            primary: '#0D9488',
            primarySoft: 'rgba(13, 148, 136, 0.065)',
            hover: 'rgba(15, 23, 42, 0.045)',
        }

    return {
        Layout: {
            siderBg: palette.nav,
            headerBg: palette.nav,
            bodyBg: palette.bg,
        },
        Card: {
            colorBgContainer: palette.surface,
            colorBorderSecondary: palette.border,
            borderRadiusLG: 8,
            boxShadowTertiary: 'none',
        },
        Button: {
            borderRadius: 8,
            controlHeight: 36,
            primaryShadow: 'none',
        },
        Menu: {
            colorBgContainer: palette.nav,
            itemBg: palette.nav,
            itemColor: palette.secondary,
            itemHoverBg: palette.hover,
            itemHoverColor: palette.text,
            itemSelectedBg: palette.primarySoft,
            itemSelectedColor: palette.primary,
            itemBorderRadius: 6,
        },
        Table: {
            headerBg: isDark ? '#203049' : '#F3F6F8',
            headerColor: isDark ? palette.secondary : '#526173',
            rowHoverBg: palette.hover,
            borderColor: palette.border,
            colorBgContainer: palette.surface,
        },
        Tabs: {
            itemSelectedColor: palette.primary,
            itemHoverColor: palette.primary,
            inkBarColor: palette.primary,
        },
        Tag: {
            borderRadiusSM: 6,
        },
        Select: {
            optionSelectedBg: palette.primarySoft,
            optionActiveBg: palette.hover,
        },
        Dropdown: {
            colorBgElevated: palette.elevated,
        },
        Modal: {
            contentBg: palette.elevated,
            headerBg: palette.elevated,
        },
        Drawer: {
            colorBgElevated: palette.elevated,
        },
    }
}

export const AppConfigProvider = ({ children }: AppConfigProviderProps) => {
    const { theme } = useThemeStore()
    const isDark = theme === 'dark'

    useEffect(() => {
        // Keep the existing attribute contract while the visual system moves to Hybrid Black / Light.
        document.documentElement.setAttribute('data-theme', `apple-${theme}`)
    }, [theme])

    const themeConfig = useMemo(() => {
        return {
            algorithm: isDark ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
            token: getStyleTokens(isDark),
            components: getComponentOverrides(isDark),
        }
    }, [isDark])

    return (
        <ConfigProvider
            locale={zhCN}
            theme={themeConfig}
        >
            {children}
        </ConfigProvider>
    )
}
