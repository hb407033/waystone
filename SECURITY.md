# Security policy

## Supported versions

| Version | Receives security fixes |
|---|---|
| 0.6.x | ✅ |
| 0.5.x | ❌ |
| Earlier versions | ❌ |

## How to report a vulnerability

Please **do not** report security issues through public Issues, discussion boards, or Pull Requests.

On this repo's page, go to **Security → Report a vulnerability** and submit through GitHub private vulnerability reporting. Please include where possible:

- Affected versions and deployment method
- Reproduction steps or a proof of concept (use your own test environment — do not attack other people's deployments)
- Your assessment of the impact, e.g. unauthorized reads across projects, bypassing proposal review, credential leaks

We'll acknowledge receipt within 7 days and credit the reporter after the fix is released (if you're willing to be named).

## In scope

- Project permissions and cross-project isolation
- Session, invitation, and device-authorization flows
- Ways to bypass proposal review, retraction, or expiry rules
- Credentials, session tokens, or memory content leaking into logs, error messages, or anywhere beyond the vector store
- Login rate limiting being bypassed

## Known, documented limitations

The following behaviors are already documented in the README's "security model and known limitations" section; reported on their own, these don't count as new vulnerabilities: credential detection only catches obvious patterns; after retraction, existing backups and Mem0's history store may still retain the original text; IPv6 clients can rotate addresses within the same subnet to bypass per-IP rate limiting; other containers on the same Docker network can reach the service directly with their own forwarding headers.
