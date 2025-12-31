# Security Policy

## Supported Versions

This project is provided as-is. Use at your own risk.

## Reporting a Vulnerability

If you believe you found a security issue, do not open a public issue with sensitive details.

- Prefer a private report to the repository owner/maintainer.
- Include a minimal repro, impact, and recommended fix if possible.

## Sensitive Data

This project can store per-account authentication data on disk.

- Never commit `accounts/` or `accounts.json`
- Never expose the Collector UI (`:8080`) to untrusted networks without adding authentication

