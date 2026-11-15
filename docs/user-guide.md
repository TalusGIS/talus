# Talus GIS documentation — where to find what

This page lists every Talus guide and runbook and says which one answers which kind of question. Start here if you do not
know where to look.

## If you use the portal

| You want to | Read |
|---|---|
| Sign in, find things in the catalog, share an item, manage groups, fill in metadata, publish to Open Data, manage your password and access tokens | `guide-portal.md` |
| Create a layer or table, add fields and domains, import a file, edit records, see a record's history, make a view layer, export data | `guide-layers.md` |
| Build a web map, style and label layers, set up popups and filters, search, select, edit on the map, print a georeferenced PDF or export a PNG, embed a map | `guide-maps.md` |
| Search for an address, set up a geocoder, build a geocoder from your own address points, geocode a table of addresses | `guide-geocoding.md` |
| Upload aerial imagery or another raster, use it in a map, download a clip of it, set the organization's basemap | `guide-imagery.md` |
| Keep a read-only copy of a table from a SQL database and refresh it on a schedule | `guide-databases.md` |
| Open Talus layers in QGIS or ArcGIS Pro, edit from desktop, work with attachments, collect in the field with QField | `connect-desktop.md` |
| Write a script against Talus | `api-guide.md` |

## If you use the Talus+ modules

| You want to | Read |
|---|---|
| Build a dashboard: indicators, charts, lists, gauges, filters, and maps over your layers | `guide-dashboards.md` |
| Build a report, schedule it, and deliver it by email or to another system; print a sheet for one record | `guide-reports.md` |
| Build a story map that pairs text and images with views of your web maps | `guide-story-maps.md` |
| Set up crews for offline field collection on phones with Talus Field | `talus-field-runbook.md` |
| Understand what Talus Field stores on a phone, what revoking does, and how external GNSS receivers work | `talus-field-devices.md` |
| Ask the Talus Helper a question, send diagnostics, or read AI usage | `guide-helper.md` |

## If you administer the portal

| You want to | Read |
|---|---|
| Add users, set roles, manage service accounts, switch public access on or off, read the health and backup pages, set organization defaults, manage the Talus+ license | `guide-administration.md` |
| Install Talus on a new server | `install-runbook.md` |
| Upgrade to a new release | `upgrade-runbook.md` |
| Install Talus+, activate or remove the AI assists, run air-gapped, renew | `talus-plus-runbook.md` |
| Restore from a backup, or recover one deleted item | `dr-restore-runbook.md` |
| Set up off-host backups you can trust | `dr-hardening.md` |
| Connect your organization's identity provider for single sign-on, or require a second factor | `sso-runbook.md` |
| Choose streamed or downloaded basemaps, and download or update the street basemap, including on a network with no outbound access | `basemap-provisioning-runbook.md` |
| Answer a security questionnaire, or learn how Talus handles sign-in, permissions, audit, encryption, and AI data | `security-overview.md` |
| Replace one of the open-source components Talus ships with | `replacing-lgpl-components.md` |

## If you are deciding whether Talus fits

| You want to | Read |
|---|---|
| A summary of what Talus is, what the two editions include, and how to install it | `README.md` |
| Answers to common questions about editing, security, scale, operations, licensing, and the AI assists | `faq.md` |

## Before you move data into Talus

Keep a backup of your source data in its original form before you import it, and keep that backup until you have checked the
data in Talus. An import makes a copy and never changes the source; the original is your way back.

## Getting help

- **Talus Community:** email `support@talusgis.com`. Every message is read by a person; the first reply may come from an automated assistant, which says so. There is no time commitment.
- **Talus+:** email `support@talusgis.com`. If you use the Talus Helper, choose **Copy for support** on its answer and
  paste that summary into your message; it carries the request ID that lets support pick up where the Helper left off.
- **Security reports:** see `security-overview.md` for how to report a vulnerability.
- **How support works for each edition, and what is and is not promised:** `support.md`.
