import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.ts', 'src/**/*.test.ts'],
    fileParallelism: false, // integration tests share one database
    testTimeout: 20_000,
    hookTimeout: 30_000, // each file rebuilds the schema and seeds every BS day from 2000 to 2090
  },
});
