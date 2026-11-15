# Talus GIS — web client

The portal's single-page app: React, TypeScript and Vite, with MapLibre GL for the map. The built
app is what the edge serves; the server it talks to is the api in `../api`.

## Develop

```bash
npm ci
npm run dev        # http://localhost:5173, against a running Talus stack
```

The api accepts this origin and the sign-in server lists it as a redirect address in a development
stack, so signing in works from the dev server.

## Check and build

```bash
npm run lint
npm run test:unit
npm run build      # token check, typecheck, production build, third-party licence notices
```

`npm run build` is the typecheck: it runs `tsc -b` before the bundle is made.

## Layout

- `src/features/` — the app by area (map, catalog, admin, open data, record detail, …)
- `src/components/`, `src/lib/` — shared pieces and the api client
- `src/styles/` — the design tokens and global styles
- `public/basemaps-assets/` — the basemap fonts and sprites, vendored so a map needs no outside host

Talus+ modules (dashboards, reports, story maps, Talus Field and the AI assists) are not in this
tree. The app loads them through one seam, `@paid`, which resolves to an inert stub here.

Licensed under AGPL-3.0-only; see `../LICENSE` and `../NOTICES`.
