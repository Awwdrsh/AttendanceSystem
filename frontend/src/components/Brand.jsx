import { Group, Text, useComputedColorScheme } from '@mantine/core';

/** CoreValley logo with the internal product name, as used across the company's own tools. */
export function Brand({ height = 22, product = true }) {
  const scheme = useComputedColorScheme('light');
  const markW = Math.round(height * (37 / 26));
  const wordW = Math.round(height * (137 / 25) * 0.86);
  return (
    <Group gap={8} wrap="nowrap" align="center" aria-label="CoreValley Hajiri">
      <Group gap={0} wrap="nowrap" align="center">
        <img
          src="/brand/cv-brandmark.svg"
          alt="CoreValley"
          width={markW}
          height={height}
          style={{ marginRight: -height * 0.24, display: 'block' }}
        />
        <img
          src={scheme === 'dark' ? '/brand/cv-wordmark-green.svg' : '/brand/cv-wordmark-carbon.svg'}
          alt=""
          aria-hidden
          width={wordW}
          height={height * 0.86}
          style={{ display: 'block' }}
        />
      </Group>
      {product ? (
        <Text
          component="span"
          className="mono"
          fz={Math.max(10, height * 0.46)}
          fw={600}
          c="var(--hj-hydro)"
          style={{
            letterSpacing: '0.06em',
            border: '1px solid var(--hj-line)',
            borderRadius: 4,
            padding: '1px 6px',
            lineHeight: 1.5,
            background: 'var(--hj-hydro-soft)',
          }}
        >
          hajiri
        </Text>
      ) : null}
    </Group>
  );
}
