# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report it privately instead, either way:

- GitHub: **Security → Report a vulnerability** on
  [kith-ai-lab/tret](https://github.com/kith-ai-lab/tret/security/advisories/new)
  (private vulnerability reporting), or
- email **contactus@kithailab.com** with "SECURITY" in the subject.

Include what you found, how to reproduce it, the version or commit you tested,
and the impact you expect. A proof of concept helps but isn't required.

We aim to acknowledge a report within 3 business days and to agree a fix and
disclosure timeline with you within 10. We credit reporters in the release
notes unless you'd rather stay anonymous.

## Supported versions

tret is pre-1.0. Security fixes land on `main` and in the next release; older
releases are not patched separately.

## Scope

In scope: the backend (`backend/`), the frontend (`frontend/`), the shipped
packs, the container images and deploy templates in this repository, and
`install.sh`.

Out of scope: vulnerabilities in the LLM providers or third-party services
tret talks to, findings that require an already-compromised admin account or
host, and self-hosted deployments that ignore the production checks in
[docs/hardening.md](docs/hardening.md) (for example, running with the shipped
development secret key).

For how tret is meant to be deployed safely, see
[docs/hardening.md](docs/hardening.md).
