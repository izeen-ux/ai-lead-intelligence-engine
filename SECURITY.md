# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository ("Security" tab → "Report a vulnerability"). You will get an answer within
7 days.

## How the project protects secrets

- API keys live only in `.env` and `secrets/` (both git-ignored) and are redacted from logs.
- The dashboard and API require an operator token outside development mode.
- Only `127.0.0.1` ports are published by Docker Compose; the sidecars are not reachable from
  outside the private Compose network.
- Paid provider calls and Google Sheet writes are off unless explicitly switched on.

If you share the dashboard through a tunnel, share the link and the token privately and stop the
tunnel when you are done.
