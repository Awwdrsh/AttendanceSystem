import { createTheme, rem, type MantineColorsTuple } from '@mantine/core';

/**
 * Visual system: a quiet product UI. Geist for everything, tabular figures for data,
 * neutral greys for chrome and one indigo accent reserved for actions and the current place.
 */
const brand: MantineColorsTuple = ['#eef0ff', '#dde0fe', '#b9bffa', '#939cf6', '#737df2', '#5f69f0', '#4f59e6', '#4048cc', '#3740b5', '#2c359f'];
const ink: MantineColorsTuple = ['#f7f7f8', '#efeff1', '#e4e5e8', '#d3d5da', '#a9adb7', '#80858f', '#5e636e', '#454953', '#2c2f36', '#17191e'];

export const theme = createTheme({
  primaryColor: 'brand',
  primaryShade: { light: 6, dark: 5 },
  colors: { brand, gray: ink, dark: ['#c9ccd3', '#a4a8b2', '#80858f', '#5e636e', '#3a3d45', '#2a2c33', '#1f2127', '#18191e', '#131418', '#0e0f12'] },
  black: '#17191e',
  fontFamily: '"Geist Variable", ui-sans-serif, system-ui, -apple-system, sans-serif',
  fontFamilyMonospace: 'ui-monospace, "SF Mono", Menlo, monospace',
  headings: {
    fontFamily: '"Geist Variable", ui-sans-serif, system-ui, sans-serif',
    fontWeight: '600',
    sizes: { h1: { fontSize: rem(28), lineHeight: '1.2' }, h2: { fontSize: rem(22), lineHeight: '1.25' }, h3: { fontSize: rem(17), lineHeight: '1.3' }, h4: { fontSize: rem(15), lineHeight: '1.35' } },
  },
  fontSizes: { xs: rem(12), sm: rem(13.5), md: rem(14.5), lg: rem(16), xl: rem(19) },
  lineHeights: { sm: '1.45', md: '1.55' },
  radius: { xs: rem(4), sm: rem(6), md: rem(8), lg: rem(12), xl: rem(16) },
  defaultRadius: 'md',
  cursorType: 'pointer',
  focusRing: 'auto',
  shadows: { xs: '0 1px 2px rgba(23,25,30,.06)', sm: '0 1px 3px rgba(23,25,30,.08)', md: '0 8px 24px -8px rgba(23,25,30,.18)' },
  components: {
    Button: { defaultProps: { radius: 'md', fw: 500 } },
    Paper: { defaultProps: { radius: 'lg' } },
    Card: { defaultProps: { radius: 'lg' } },
    Badge: { defaultProps: { radius: 'sm', tt: 'none', fw: 500 } },
    Table: { defaultProps: { fz: 'sm', highlightOnHover: true, verticalSpacing: 10, horizontalSpacing: 'md' } },
    TextInput: { defaultProps: { radius: 'md' } },
    Select: { defaultProps: { radius: 'md' } },
    Modal: { defaultProps: { radius: 'lg', overlayProps: { backgroundOpacity: 0.35, blur: 2 } } },
    Tooltip: { defaultProps: { withArrow: false, openDelay: 250 } },
    Tabs: { defaultProps: { variant: 'default' } },
  },
});
