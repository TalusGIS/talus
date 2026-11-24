# Contributing to Talus GIS

Thank you for your interest in Talus — genuinely. Please read this short page before sending
anything, because our contribution model is deliberately unusual.

## Talus does not accept outside code contributions

Talus is **solo-maintained by design**. Every capability in this codebase went through a
written design decision before a line of code was written, ships with its own verification
sweep and security negative-tests, and is built against a deliberately narrow product thesis
(see the README). That discipline — one owner, decision-doc-before-code, a security review of
every change — *is* the engineering model, and drive-by code changes don't compose with it.

There is also a legal dimension: the platform core is AGPLv3 with commercial licensing
available, and that dual-licensing arrangement requires a **single copyright holder with
complete rights over the codebase**. Accepting outside code would require contributor license
agreements and review overhead that a solo project is better off without.

**Pull requests are therefore closed automatically**, regardless of quality — it's policy,
not a judgment of your work. (The precedent here is SQLite, which has operated this way,
respectably, for decades.)

## What is genuinely welcome

- **Bug reports** — a reproducible bug report is the most valuable thing you can send.
  Include your deployment shape (compose profile, browser/client, layer type) and exact
  steps. Post it in **GitHub Discussions** (Q&A) or email **`support@talusgis.com`** — this
  repository does not use GitHub Issues.
- **Security reports** — please **do not** send suspected vulnerabilities to `support@`;
  see `SECURITY.md` for private disclosure (`security@talusgis.com`, or GitHub's private
  vulnerability reporting on this repository).
- **Feature requests and use-case reports** — especially from municipal GIS practitioners.
  "Here is the workflow I do in ArcGIS Enterprise and how it maps (or doesn't) to Talus" is
  exactly the input that steers the roadmap. Post it in **GitHub Discussions** (Ideas) or
  email `support@talusgis.com`.
- **Interop findings** — Talus speaks OGC API Features/WMTS/WCS and GeoServices to real
  desktop clients. If a client misbehaves against a Talus endpoint, a capture of the request
  pattern is gold.
- **Documentation feedback** — unclear docs are bugs; report them the same way.

## Forks

The AGPLv3 license means you are fully free to fork, modify, and run your own Talus —
including for your own organization's production use — subject to the license's terms. The
"Talus GIS" name and mark are not licensed for use on modified versions.

## Might this policy ever change?

Possibly. If it does, code contributions will require a signed Contributor License Agreement,
and this page will be updated first. Until then: email, not PRs.
