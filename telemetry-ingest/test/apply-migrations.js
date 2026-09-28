import { applyD1Migrations, env } from 'cloudflare:test';
import { beforeEach } from 'vitest';

// vitest-pool-workers 0.6 gave every test its own copy of storage; the
// vitest 4 releases dropped per-test isolated storage, so rows would carry
// over from one test to the next inside a file. Rebuild the database from
// migrations/ before each test instead: drop every table (children before
// parents, i.e. reverse creation order, so foreign keys never object; the
// migrations bookkeeping table included) and re-apply the migrations, which
// also restores the rows they seed.
beforeEach(async () => {
  const { results } = await env.DB.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' " +
      "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' ORDER BY rowid DESC",
  ).all();
  if (results.length) {
    await env.DB.batch(results.map(({ name }) => env.DB.prepare(`DROP TABLE "${name}"`)));
  }
  await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
});
