import { applyD1Migrations, env } from 'cloudflare:test';

// Applies migrations/0001_init.sql (etc.) to the local D1 instance before
// each test file runs, per the documented vitest-pool-workers D1 pattern.
await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
