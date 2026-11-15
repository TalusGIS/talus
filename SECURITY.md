# Security Policy

Talus GIS treats trust as a core product commitment: secure-by-default architecture, honest
communication about gaps, and no overclaiming. This policy covers how to report a
vulnerability and what to expect from us.

## Reporting a vulnerability

**Please report a security vulnerability privately, through one of the two channels below — not in a pull
request or any other public place.** This repository does not use GitHub Issues.

- **Preferred channel:** email **`security@talusgis.com`**. It is read by a person — no
  automation, no AI, nothing between your message and the maintainer. GitHub's **private
  vulnerability reporting** on this repository works too.
- Include what you can: affected endpoint/component, reproduction steps, impact as you
  understand it, and any proof-of-concept. Partial reports are welcome — do not hold a report
  back because it is incomplete.

## What to expect

- **Acknowledgement** of your report within **3 business days**. This applies to every report, from anyone.
  Talus+ customers also have the support response times in `docs/support.md`; those are in addition to this
  commitment, not a replacement for it.
- An assessment and a remediation plan (or an honest "we need longer, here's why") within
  **14 days** of acknowledgement.
- Credit in the fix's release notes if you want it (say so in your report); anonymity if you
  prefer.
- No legal action for good-faith research: testing against **your own deployment** is always
  in scope. Do not test against deployments you do not own or operate, and do not access,
  modify, or exfiltrate data that is not yours.

## Scope notes

- Talus is **self-hosted** software: there is no hosted Talus deployment operated by this project to
  test against. Reports should be reproducible against a stock deployment (installed with
  `scripts/talus-setup.sh`; see `docs/install-runbook.md`).
- One service is operated by Talus GIS LLC: the gateway that the optional AI assists in Talus+ call
  (described in `docs/security-overview.md`). It is not a test target. If you believe you have found a
  problem in it, report it to the address above rather than probing it.
- The security-relevant design is documented, not secret: fail-closed authorization
  (403/503, never leak-allow), item-level OpenFGA authz, first-class write-audit with actor
  attribution, no request signed in with a password (a desktop client that speaks HTTP Basic presents a
  revocable access token, never the account password), secrets generated per-deployment at install
  time, and encrypted-at-rest stored credentials. Reports that show any of these properties failing are
  exactly what we most want to hear about.
- Every data-serving path ships with negative tests (`scripts/verify-invariants/`); a
  confirmed report typically becomes a new invariant there alongside its fix.

## Supported versions

| Version | Security fixes |
|---|---|
| 1.0.x (the current line) | Yes |
| Releases before 1.0.0 | No — upgrade to the latest 1.0.x release |

A security fix ships as a patch release in the 1.0.x line (releases are tagged, digest-pinned and carry
SBOMs — see the Releases page and `CHANGELOG.md`). Run the latest 1.0.x release: fixes are not
back-ported to earlier patch releases, and there is no other maintained release branch.

## Honesty statement

Talus has strong security *architecture* and **no formal compliance certifications yet** (no
SOC 2, StateRAMP, FedRAMP, or CJIS). Do not rely on this project to satisfy a compliance
checkbox it has not earned. The security architecture, and a plain list of what Talus does not
have yet, are in [`docs/security-overview.md`](docs/security-overview.md).
