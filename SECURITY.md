# Security policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue or pull
request:

- **GitHub:** the **Report a vulnerability** button on this repository's
  *Security* tab (private vulnerability reporting), or
- **Email:** hello@annotide.com, with "Security" in the subject.

Include what you found, the version or commit, steps to reproduce and the
impact you expect. A proof of concept helps; please don't access data that
isn't yours or degrade a service while testing.

## What happens next

Annotide is maintained by a small team, so these are targets, not guarantees:

- an acknowledgement within 5 business days;
- an assessment, and a fix plan for confirmed issues, within 10 business days;
- a fixed release, then a GitHub security advisory crediting you unless you
  prefer otherwise.

We ask you to keep the issue private until a fix is released, or for 90 days
from your report, whichever comes first. We don't take legal action against
good-faith research that follows this policy. There is no bug bounty.

## Supported versions

Security fixes go into the latest release. Self-hosted installs should stay on
it: `docs/OPERATIONS.md` covers upgrading.

## Scope

This repository: the application, the Helm chart, the Terraform modules, the
Personal install script, the Python SDK and the reference model service.
Installations are run by their operators, so a misconfiguration of a
particular deployment is a matter for its operator.

[`docs/SECURITY.md`](docs/SECURITY.md) describes the architecture and trust
boundaries for security reviewers and penetration testers.
