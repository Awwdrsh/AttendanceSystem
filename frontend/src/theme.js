import { createTheme, rem } from '@mantine/core';

/**
 * CoreValley visual system: "hydro" green for actions and the current place, cool carbon greys for surfaces,
 * ink for text. Manrope for reading, JetBrains Mono for small technical labels and codes.
 */
const hydro = ['#d6fbe5', '#a7f3cb', '#6ee7a8', '#4ade80', '#22c55e', '#16a34a', '#15803d', '#12773a', '#166534', '#14532d'];
const ink = ['#f3f5f7', '#eaeef2', '#e6eaef', '#d9dee5', '#c5cbd3', '#868d9a', '#5b6472', '#4b5563', '#2b3340', '#161c26'];
const carbon = ['#e8ecef', '#c2c8cf', '#9aa2ad', '#868d9a', '#333944', '#232b38', '#181e29', '#0b0f17', '#080c13', '#05080d'];

export const theme = createTheme({
  primaryColor: 'brand',
  primaryShade: { light: 7, dark: 3 },
  autoContrast: true,
  colors: { brand: hydro, green: hydro, gray: ink, dark: carbon },
  black: '#161c26',
  fontFamily: '"Manrope Variable", Manrope, system-ui, -apple-system, "Segoe UI", sans-serif',
  fontFamilyMonospace: '"JetBrains Mono Variable", "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  headings: {
    fontFamily: '"Manrope Variable", Manrope, system-ui, sans-serif',
    fontWeight: '700',
    sizes: {
      h1: { fontSize: rem(28), lineHeight: '1.15' },
      h2: { fontSize: rem(22), lineHeight: '1.2' },
      h3: { fontSize: rem(17), lineHeight: '1.3' },
      h4: { fontSize: rem(15), lineHeight: '1.35' },
    },
  },
  fontSizes: { xs: rem(12), sm: rem(13.5), md: rem(14.5), lg: rem(16), xl: rem(19) },
  lineHeights: { sm: '1.5', md: '1.6' },
  radius: { xs: rem(2), sm: rem(4), md: rem(6), lg: rem(8), xl: rem(12) },
  defaultRadius: 'md',
  cursorType: 'pointer',
  focusRing: 'auto',
  shadows: {
    xs: '0 1px 2px #0f172a14',
    sm: '0 1px 2px #0f172a14',
    md: '0 4px 16px #0f172a1a',
    lg: '0 12px 40px #0f172a24',
    xl: '0 24px 70px #0f172a2e',
  },
  components: {
    Button: { defaultProps: { radius: 'md', fw: 600 } },
    Paper: { defaultProps: { radius: 'lg' } },
    Card: { defaultProps: { radius: 'lg' } },
    Badge: { defaultProps: { radius: 'sm', tt: 'none', fw: 600 } },
    Table: { defaultProps: { fz: 'sm', highlightOnHover: true, verticalSpacing: 10, horizontalSpacing: 'md' } },
    TextInput: { defaultProps: { radius: 'md' } },
    Select: { defaultProps: { radius: 'md' } },
    Modal: { defaultProps: { radius: 'lg', overlayProps: { backgroundOpacity: 0.3, blur: 3 } } },
    Tooltip: { defaultProps: { withArrow: false, openDelay: 250 } },
    Tabs: { defaultProps: { variant: 'default' } },
  },
});
