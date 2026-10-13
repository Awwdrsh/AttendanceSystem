import { createTheme, rem } from '@mantine/core';

/** Neutral chrome, one accent (teal), Inter for Latin, Noto Sans Devanagari for Nepali (plan §13.2, §13.11). */
export const theme = createTheme({
  primaryColor: 'teal',
  fontFamily: 'Inter, "Noto Sans Devanagari", system-ui, sans-serif',
  headings: { fontFamily: 'Inter, "Noto Sans Devanagari", system-ui, sans-serif', fontWeight: '600' },
  fontSizes: { xs: rem(12), sm: rem(14), md: rem(16), lg: rem(18), xl: rem(20) },
  lineHeights: { md: '1.7' },
  defaultRadius: 'md',
  components: {
    Button: { defaultProps: { radius: 'md' } },
    Table: { defaultProps: { fz: 'sm', highlightOnHover: true, verticalSpacing: 'xs' } },
  },
});
