import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.ts', 'src/**/*.test.ts'],
    fileParallelism: false, // integration tests share one database
    testTimeout: 20_000,
  },
});
