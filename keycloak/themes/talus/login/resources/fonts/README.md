# Talus login theme — fonts

The login CSS (`../css/talus-login.css`) references two self-hosted variable fonts so the sign-in page
matches the portal **and** works **air-gapped** (no CDN, no outbound request — same posture as the SPA,
which self-hosts via `@fontsource-variable`):

- `InterVariable.woff2`        → `font-family: 'Inter Variable'`
- `JetBrainsMono.woff2`        → `font-family: 'JetBrains Mono Variable'`

These binaries are **not committed** (we don't vendor font blobs into the repo). Drop them here before
building the image / mounting the theme:

```bash
# From the web workspace (fonts come in with the SPA deps):
cp web/node_modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2 \
   keycloak/themes/talus/login/resources/fonts/InterVariable.woff2
cp web/node_modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2 \
   keycloak/themes/talus/login/resources/fonts/JetBrainsMono.woff2
```

(Exact filenames vary by `@fontsource-variable` version — `ls` the `files/` dir if the copy misses.)

**If the files are absent the page still renders correctly** — the CSS falls back to
`system-ui / -apple-system / Segoe UI / sans-serif`, and `font-display: swap` means no blocking and no
network attempt. Adding the woff2s is a polish step, not a correctness requirement.
