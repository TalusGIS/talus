# Support

How support for Talus GIS works: where to write, what each edition gets, and what is and is not promised.

For Talus+ customers, the signed agreement governs.

## Where to write

- **Support and troubleshooting:** `support@talusgis.com`. Include your release version and, if you have one,
  the request id from the Talus Helper's support summary. The first reply may come from an automated assistant,
  so you get an answer quickly; it says it is AI-generated, and you can ask for a person to follow up.
- **Security reports:** `security@talusgis.com`, or the repository's private vulnerability reporting. This mailbox
  is read by a person only. See `SECURITY.md`.
- **Sales and general questions:** `info@talusgis.com`.
- **Bug reports in public:** GitHub Issues, `https://github.com/TalusGIS/talus/issues`.
- **Questions, ideas and feedback in public:** GitHub Discussions,
  `https://github.com/TalusGIS/talus/discussions`.

Both are read by a person. Never post a security problem in either.

## Talus Community (free)

- **Channel:** email, `support@talusgis.com`, GitHub Issues for bugs, or GitHub Discussions.
- **Response commitment:** none. Every message is read by a person and answered when that is feasible. Bugs are
  fixed in point releases, not parked.
- **Documentation:** public and kept current. It is what makes running Community practical: start at
  `docs/user-guide.md` and the runbooks.

## Talus+ (paid)

- **Channel:** email, `support@talusgis.com`, with scheduled video for a production-impacting incident.
- **Response commitments,** measured on the clock defined below:

| Severity | What it means | Response |
|---|---|---|
| Acknowledgment | Any request | Within one business day |
| **Sev-1**, production-impacting | The platform is down or a critical workflow is broken | The same business day (see below) |
| **Sev-2**, functional | Something is broken but the platform is usable | Within one business day |
| **Sev-3**, informational | Questions, configuration help, clarifications | Within three business days |

- **Video for a Sev-1:** you can ask for a video session the same business day, scheduled within business hours.
- **A quarterly check-in,** offered and not required: a 30-minute call about recent changes and what is coming.
- **Covered:** platform issues, configuration questions, deployment troubleshooting, and integration questions
  about the platform itself.
- **Not covered:** problems in your own data, training, and custom development.
- **Canonical code only:** support applies to deployments running the code Talus GIS LLC distributes. This is a
  condition of support, not of the licence.

## The clock

- **Business hours:** 9:00 AM to 5:00 PM US Eastern Time, Monday through Friday, excluding US federal holidays
  and two weekdays between December 24 and January 1 that Talus names each year by December 1.
- **Receipt:** a request that arrives outside business hours counts as received at the start of the next
  business day.
- **Response:** a substantive reply from a person, not an automatic acknowledgment. **A response is not a
  resolution.** Nothing here is a time-to-fix; resolution is as soon as practical.
- **Same business day (Sev-1):** a request received by 2:00 PM Eastern is answered and actively worked before
  the end of that day's business hours. One received after 2:00 PM is answered by noon the next business day.

## Releases and fixes

- **Security fixes are published, not held.** A security fix goes out as a patch release as soon as it is
  ready, not at the next feature release. Talus+ customers are told by email when it is published. A reported
  vulnerability is worked as a Sev-1.
- **Other fixes** arrive in point releases, typically quarterly.
- **A Talus+ customer is never left without security patches during a paid term.**

## What support is not

- **No around-the-clock support.** A request made overnight is answered in the morning.
- **No uptime guarantee for a self-hosted deployment.** You run the server; the commitment here is response.
- **No on-site visits** as a standard offering.
- **No managed hosting.** Every Talus deployment is self-hosted.
- **No support for modified forks.**
- **If Talus+ lapses, nothing installed is disabled.** You keep the open-source core and every Talus+ module
  already installed, in the versions you received. What ends is updates (security patches included), support, and the AI assists.
