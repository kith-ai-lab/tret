<!-- Thanks for contributing to bench. Keep this short; the diff says the rest. -->

## What and why

<!-- What changes, and what problem it solves. Link an issue if there is one. -->

## Checks

```bash
cd backend && .venv/bin/ruff check bench tests && .venv/bin/pytest -q
cd frontend && npm run build
```

- [ ] Lint and the full backend suite pass (this includes `tests/evals`)
- [ ] Frontend builds, if I touched it

## Golden-run policy

`backend/tests/evals/` locks in the trust guarantees from the README. Prompt,
doctrine, routing, provider, and pack changes must keep them green
([docs/evals.md](../blob/main/docs/evals.md), CONTRIBUTING.md).

- [ ] The golden runs pass unchanged, **or** I changed an expectation in this
      same commit and explained why below — and did not loosen it (no dropped
      assertion, no removed failure text, no widened set)
- [ ] If this PR adds a guarantee, it adds the scenario that protects it

<!-- If you changed a golden expectation, justify it here: -->

## Trust guarantees

- [ ] No path added by which a model can introduce a number that was not
      retrieved or computed by a vetted method
- [ ] Nothing reaches `approved` without a named human from the login session
- [ ] Nothing was removed from the run audit trail

## Notes for reviewers

<!-- Migrations (single Alembic head?), new config values, docs updated, anything
     deliberately left out of scope. -->
