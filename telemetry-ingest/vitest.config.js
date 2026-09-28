// Runs tests against a real local D1 instance (via Miniflare, inside
// workerd) using @cloudflare/vitest-pool-workers, so behavior that depends
// on SQLite specifics -- the unique-constraint 429, the ON CONFLICT
// upserts, D1's batch-as-transaction semantics -- is exercised for real
// rather than against a hand-rolled mock.
import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-pool-workers';
import { defineConfig } from 'vitest/config';
import path from 'node:path';

const migrationsPath = path.join(__dirname, 'migrations');
const migrations = await readD1Migrations(migrationsPath);

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
      miniflare: {
        // Handed to the worker environment as env.TEST_MIGRATIONS so
        // test/apply-migrations.js can apply them before each test file
        // runs. Not a real binding the Worker's own code ever reads.
        bindings: { TEST_MIGRATIONS: migrations },
      },
    }),
  ],
  test: {
    setupFiles: ['./test/apply-migrations.js'],
  },
});
