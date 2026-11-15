import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router'

import {
  apiOrigin,
  ApiError,
  downloadLayerExport,
  fetchLayerColumns,
  fetchLayerEditing,
  fetchMyTokens,
  fetchOrgSettings,
  type MyLayer,
  type PatMinted,
  type PatSummary,
} from '@/lib/api'
import { MintTokenModal } from '@/features/profile/AccessTokens'
import { ProServerDialogNote } from '@/features/catalog/ProServerDialogNote'
import { buildProOgcFile, buildQgisConnectionsXml, downloadTextFile } from '@/lib/connectionFiles'
import CloseButton from '@/components/CloseButton'
import Caret from '@/components/Caret'

/**
 * The "Use in…" wizard (Interop I-1 frontend, decision-desktop-interop-i1-frontend.md §2) — the
 * coherence layer made visible, reshaped 2026-07-05 into the CreateLayerWizard
 * paged pattern: intro (the two-step model, same voice as the Profile page) → goal → recipe, with
 * the Cancel · Back · Next · Finish button bar.
 *
 * Recipe philosophy after the review: the CONNECTION FILE is the default workflow (token, file,
 * done); the server URL is a deployment fact, not a user input — it appears only inside the
 * "set it up manually" expandable. Testing with an existing token is likewise collapsed.
 *
 * The two doors (decision-desktop-interop-i1.md): `/ogc-auth` (SIGN-IN) for every signed-in
 * desktop client; `/ogc` (PUBLIC) for anonymous/harvester/script. URLs derive from the admin-set
 * advertised base (V063) with the SPA's API base as fallback.
 *
 * `UseInContent` is the embeddable body (the Manage dialog's "Use in…" tab); `UseInWizard` wraps
 * it in the house modal for the catalog-row entry point.
 */

type Goal = 'pro' | 'qgis' | 'edit-pro' | 'edit-qgis' | 'script' | 'download'
type Page = 'intro' | 'goals' | 'recipe'

const GOALS: Array<{
  id: Goal
  label: string
  hint: string
  soon?: string
}> = [
  { id: 'pro', label: 'View / analyze in ArcGIS Pro', hint: 'live read-only layer over the network' },
  { id: 'qgis', label: 'View / analyze in QGIS', hint: 'live read-only layer over the network' },
  { id: 'edit-pro', label: 'Edit in ArcGIS Pro', hint: 'live editing — paste one link (I-3)' },
  { id: 'edit-qgis', label: 'Edit in QGIS', hint: 'live editing — paste one link (I-3)' },
  { id: 'script', label: 'Use in a script or web app', hint: 'OGC API — Features, plain JSON' },
  { id: 'download', label: 'Download a copy', hint: 'GeoPackage · GeoJSON · CSV' },
]

/** A collapsed detail section — the wizard's "if you need it, it's here" affordance. */
function Expandable({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="use-in__expand">
      <button type="button" className="use-in__expand-toggle" onClick={() => setOpen((o) => !o)}>
        <Caret open={open} /> {label}
      </button>
      {open && <div className="use-in__expand-body">{children}</div>}
    </div>
  )
}

// --- the guided checklist (design-handoff 1a) ------------------------
// One shared shell for every step-shaped recipe: numbered rail, connector lines, current step in
// accent amber. Icons are tiny inline SVGs (Lucide-style strokes, NO icon dependency).
// The mint MODAL stays the one flow everywhere (fork 6 option A) — the
// checklist's "Create token" button opens it; the success block then shows the minted secret.

const _SVG = {
  width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
}
const IconPlus = () => (<svg {..._SVG}><path d="M12 5v14M5 12h14" /></svg>)
const IconKey = () => (
  <svg {..._SVG} width={17} height={17}>
    <circle cx="16.5" cy="7.5" r="4.5" />
    <path d="M13.3 10.7 4 20M4 20v-3M7 17l2 2" />
  </svg>
)
const IconCopy = () => (
  <svg {..._SVG} width={14} height={14}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
)
const IconCheck = () => (<svg {..._SVG} width={12} height={12} strokeWidth={3}><path d="M20 6 9 17l-5-5" /></svg>)
const IconZap = () => (<svg {..._SVG} width={15} height={15}><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" /></svg>)

function Step({
  n, title, optional, current, last, children,
}: {
  n: number
  title: string
  optional?: boolean
  current: number
  last?: boolean
  children: ReactNode
}) {
  return (
    <div className={last ? 'ck__row ck__row--last' : 'ck__row'}>
      <div className="ck__rail">
        <span className={n === current ? 'ck__badge ck__badge--now' : 'ck__badge'}>{n}</span>
        {!last && <span className="ck__line" />}
      </div>
      <div className="ck__content">
        <h4 className="ck__title">
          {title}
          {optional && <span className="ck__optional">(Optional)</span>}
        </h4>
        {children}
      </div>
    </div>
  )
}

/** Mono value + copy icon-button (flashes to a check). The checklist's URL/secret affordance. */
function CopyField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="ck__field">
      <code>{value}</code>
      <button
        type="button"
        className="ck__iconbtn"
        aria-label={copied ? 'Copied' : 'Copy'}
        title={copied ? 'Copied' : 'Copy'}
        onClick={() => {
          void navigator.clipboard?.writeText(value)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        {copied ? <IconCheck /> : <IconCopy />}
      </button>
    </div>
  )
}

/** Step 1's body: default = Create button + the existing-tokens helper; minted = the success
 *  block. The button opens the SHARED MintTokenModal (scope selector and all — one flow
 *  everywhere); `editing` recipes preselect read_write and count only read-write tokens as
 *  "would work". */
function TokenStep({
  accessToken, myTokens, minted, setMinted, editing, appName,
}: {
  accessToken: string
  myTokens: PatSummary[]
  minted: PatMinted | null
  setMinted: (m: PatMinted | null) => void
  editing: boolean
  /** Client label for the token-name example placeholder (e.g. "ArcGIS Pro"). */
  appName: string
}) {
  const [showMint, setShowMint] = useState(false)
  const usable = editing ? myTokens.filter((t) => t.scope === 'read_write') : myTokens
  return (
    <>
      {minted === null ? (
        <div className="ck__stack">
          <button
            type="button"
            className="btn"
            style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
            onClick={() => setShowMint(true)}
          >
            <IconPlus /> {editing ? 'Create Read & edit token' : 'Create token'}
          </button>
          <div className="ck__helper">
            <IconKey />
            <span>
              {usable.length > 0 && (
                <>
                  You already have <strong>{usable.length} token{usable.length === 1 ? '' : 's'}</strong>{' '}
                  that would work —{' '}
                  {usable.map((t, i) => (
                    <span key={t.id}>
                      {i > 0 && ' '}
                      <span className="ck__chip">{t.label}</span>
                    </span>
                  ))}
                  {'. '}
                </>
              )}
              {editing && 'Editing needs a Read & edit token — read-only tokens are refused. '}
              A token is your identity, not a key to this layer — sharing still decides what it
              can see and edit. View and revoke tokens in your <Link to="/profile">Profile</Link>.
            </span>
          </div>
        </div>
      ) : (
        <div className="ck__success">
          <div className="ck__success-head">
            <span className="ck__success-dot"><IconCheck /></span>
            Token created — {minted.label}
          </div>
          <CopyField value={minted.token} />
          <p className="ck__note">
            <span>
              Copy the secret now — you won’t see it again. It’s the password when the app asks
              (username <code className="sa-code">talus</code>).
            </span>
            <button type="button" className="ck__linkbtn" onClick={() => setMinted(null)}>
              Create another
            </button>
          </p>
        </div>
      )}
      {showMint && (
        <MintTokenModal
          accessToken={accessToken}
          defaultScope={editing ? 'read_write' : 'read_only'}
          namePlaceholder={`e.g. ${appName} on my work laptop`}
          onClose={() => setShowMint(false)}
          onMinted={(m) => {
            setShowMint(false)
            setMinted(m)
          }}
        />
      )}
    </>
  )
}

/** The Verify step: accent Test button + status pill (Not tested / Testing… / Connected /
 *  Failed). Same probe + honest messages as before; reports success up so the recipe can
 *  advance the current-step highlight. */
function TestStep({
  url, externalToken, requireToken, anonNote, onResult,
}: {
  url: string
  /** A token minted in step 1 ('' = none — the paste input appears when one is required). */
  externalToken: string
  requireToken: boolean
  anonNote?: string
  onResult?: (ok: boolean) => void
}) {
  const [pasted, setPasted] = useState('')
  const [state, setState] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle')
  const [msg, setMsg] = useState('')
  const token = externalToken || pasted.trim()

  async function run(): Promise<void> {
    setState('testing')
    setMsg('')
    try {
      const headers: Record<string, string> = { Accept: 'application/json' }
      if (token) headers.Authorization = `Basic ${btoa(`talus:${token}`)}`
      const res = await fetch(url, { headers, cache: 'no-store' })
      if (res.ok) {
        setState('ok')
        setMsg('The layer is visible with these credentials.')
        onResult?.(true)
        return
      }
      setState('fail')
      if (res.status === 401) {
        setMsg('Reached the server, but the token was rejected (revoked, expired, or mistyped).')
      } else if (res.status === 403 || res.status === 404) {
        setMsg(`Reached the server, but the layer isn’t visible with these credentials${anonNote ? ` (${anonNote})` : ''}.`)
      } else {
        setMsg(`Server answered ${res.status} — check the URL.`)
      }
      onResult?.(false)
    } catch {
      setState('fail')
      setMsg('Couldn’t reach the server from this browser. If the URL is meant for another machine (a LAN address), test from there.')
      onResult?.(false)
    }
  }

  return (
    <div className="ck__stack">
      {requireToken && !externalToken && (
        <>
          <p className="modal__muted" style={{ margin: 0 }}>
            Paste a token you saved earlier to test with it (tokens are shown only once at
            creation).
          </p>
          <input
            type="password"
            className="use-in__paste"
            placeholder="tpat_…"
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            autoComplete="off"
          />
        </>
      )}
      <div className="ck__pillrow">
        <button
          type="button"
          className="btn btn--sm btn--accent"
          style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
          disabled={state === 'testing' || (requireToken && !token)}
          title={requireToken && !token ? 'Create a token (or paste an existing one) first' : undefined}
          onClick={() => void run()}
        >
          <IconZap /> Test connection
        </button>
        {state === 'idle' && <span className="ck__pill">Not tested</span>}
        {state === 'testing' && <span className="ck__pill">Testing…</span>}
        {state === 'ok' && <span className="ck__pill ck__pill--ok">Connected</span>}
        {state === 'fail' && <span className="ck__pill ck__pill--fail">Failed</span>}
        {msg && (state === 'ok' || state === 'fail') && <span className="ck__testmsg">{msg}</span>}
      </div>
      <p className="modal__muted" style={{ margin: 0 }}>
        Tests from this browser — a desktop on another machine must be able to reach the same
        address.
      </p>
    </div>
  )
}

export function UseInContent({
  layer,
  accessToken,
  isTable,
  onClose,
}: {
  layer: MyLayer
  accessToken: string
  /** geomType 'none': tables have no OGC story in v1 — desktop goals render gated. */
  isTable: boolean
  /** Cancel/Finish target — the wizard modal's close (or the Manage dialog's, when embedded). */
  onClose: () => void
}) {
  const [page, setPage] = useState<Page>('intro')
  const [goal, setGoal] = useState<Goal | null>(null)
  const [advertised, setAdvertised] = useState<string | null>(null)
  // Editing flag: readable only by owner/admin — best-effort, null = unknown (gate not shown).
  const [editingOn, setEditingOn] = useState<boolean | null>(null)
  // Attachments flag: view-gated via /columns (any viewer) — drives the QGIS attachments step
  // and Pro's "ignore the extra field" note. null = unknown.
  const [attachOn, setAttachOn] = useState<boolean | null>(null)
  const [myTokens, setMyTokens] = useState<PatSummary[]>([])

  useEffect(() => {
    let active = true
    fetchOrgSettings(accessToken)
      .then((s) => active && setAdvertised(s.advertisedBaseUrl))
      .catch(() => active && setAdvertised(null))
    fetchMyTokens(accessToken)
      .then((ts) => active && setMyTokens(ts.filter((t) => t.revoked_at === null)))
      .catch(() => active && setMyTokens([]))
    if (layer.canManage) {
      fetchLayerEditing(layer.id, accessToken)
        .then((v) => active && setEditingOn(v.editingEnabled))
        .catch(() => active && setEditingOn(null))
    }
    fetchLayerColumns(layer.id, accessToken)
      .then((c) => active && setAttachOn(c.attachmentsEnabled ?? false))
      .catch(() => active && setAttachOn(null))
    return () => {
      active = false
    }
  }, [layer.id, layer.canManage, accessToken])

  const base = (advertised ?? apiOrigin()).replace(/\/+$/, '')
  const authDoor = `${base}/api/v1/ogc-auth`
  const publicDoor = `${base}/api/v1/ogc`
  // The GeoServices (Esri REST) sign-in door — the per-layer copy-link + editing dialect (I-3).
  // TWO granularities, per client (a live QGIS test): Pro's Add Data From
  // Path wants the LAYER (…/FeatureServer/0); a QGIS ArcGIS-REST CONNECTION wants the SERVICE
  // (…/FeatureServer) — its browser expands a service listing, and a /0 URL shows an empty
  // connection with nothing addable.
  const esriServiceUrl = `${base}/api/v1/esri-auth/rest/services/${layer.id}/FeatureServer`
  const esriLayerUrl = `${esriServiceUrl}/0`
  // The services ROOT — ONE QGIS ArcGIS-REST connection here browses the whole viewable catalog
  // (proven headless, scripts/qgis-afs/afs_browse_root.py): every layer, current and future, is
  // then "expand and add" with zero further setup — the closest thing to AGO's "it's already
  // there". The per-layer link is for pasting one item; this is for connecting the computer once.
  const esriCatalogUrl = `${base}/api/v1/esri-auth/rest/services`

  return (
    <>
      {page === 'intro' && (
        <section className="modal__section">
          <p>
            Connecting a desktop GIS (ArcGIS Pro, QGIS) takes two steps, once per computer:{' '}
            <strong>1</strong> — create an access token (your app password), <strong>2</strong> —
            download and import the connection file, then sign in with the token when the app
            asks.
          </p>
          <p className="modal__muted">
            After that, every layer you can view — including this one — is simply there in the
            app, and layers shared to you later appear on their own. Click Next to pick what you
            want to do with “{layer.displayName ?? layer.id}”.
          </p>
          {advertised === null && (
            <p className="modal__muted">
              Connection URLs currently use this portal’s API address. An administrator can set
              the organization’s advertised base URL under Admin → Connections so they always show an
              address other machines can reach.
            </p>
          )}
        </section>
      )}

      {page === 'goals' && (
        <section className="modal__section">
          <h3 className="modal__section-title">I want to…</h3>
          <div className="use-in__goals">
            {GOALS.map((g) => {
              const gated = isTable && g.id !== 'download'
              return (
                <button
                  key={g.id}
                  type="button"
                  className="use-in__goal"
                  disabled={!!g.soon || gated}
                  title={
                    g.soon
                      ? 'Coming in a future release'
                      : gated
                        ? 'Tables aren’t served over OGC yet — download a copy instead'
                        : undefined
                  }
                  onClick={() => {
                    setGoal(g.id as Goal)
                    setPage('recipe')
                  }}
                >
                  <span className="use-in__goal-label">{g.label}</span>
                  <span className="modal__muted">{g.hint}</span>
                  {g.soon && <span className="sa-badge sa-badge--off">Soon</span>}
                </button>
              )
            })}
          </div>
        </section>
      )}

      {page === 'recipe' &&
        (goal === 'download' ? (
          <DownloadRecipe layer={layer} accessToken={accessToken} isTable={isTable} />
        ) : goal === 'script' ? (
          <ScriptRecipe layer={layer} publicDoor={publicDoor} authDoor={authDoor} />
        ) : goal === 'edit-pro' || goal === 'edit-qgis' ? (
          <EditRecipe
            client={goal === 'edit-pro' ? 'pro' : 'qgis'}
            layer={layer}
            esriServiceUrl={esriServiceUrl}
            esriLayerUrl={esriLayerUrl}
            esriCatalogUrl={esriCatalogUrl}
            chuteBase={`${base}/api/v1/esri-auth/layers/${layer.id}/attachments`}
            accessToken={accessToken}
            myTokens={myTokens}
            editingOn={editingOn}
            attachOn={attachOn}
          />
        ) : goal ? (
          <DesktopRecipe
            client={goal}
            layer={layer}
            authDoor={authDoor}
            esriServiceUrl={esriServiceUrl}
            esriLayerUrl={esriLayerUrl}
            accessToken={accessToken}
            myTokens={myTokens}
            editingOn={editingOn}
          />
        ) : null)}

      {/* Button bar (00-shared-conventions §3): Cancel = neutral ghost, Back =
          secondary, Next/Finish = primary brand. Red is reserved for destructive
          actions only (Delete/Revoke), so Cancel is no longer danger-red and
          Finish is no longer a bespoke green — both now ride the DS button system. */}
      <div className="sign-form__actions wizard__actions">
        <button type="button" className="btn btn--sm btn--ghost" onClick={onClose}>
          Cancel
        </button>
        <span className="wizard__actions-spacer" />
        {page !== 'intro' && (
          <button
            type="button"
            className="btn btn--sm btn--secondary"
            onClick={() => setPage(page === 'recipe' ? 'goals' : 'intro')}
          >
            Back
          </button>
        )}
        {page === 'intro' && (
          <button type="button" className="btn btn--sm" onClick={() => setPage('goals')}>
            Next
          </button>
        )}
        {page === 'recipe' && (
          <button type="button" className="btn btn--sm" onClick={onClose}>
            Finish
          </button>
        )}
      </div>
    </>
  )
}

/** The honest inline gates (kickoff §3.2): real layer state, plainly stated. */
function Gates({ layer, editingOn }: { layer: MyLayer; editingOn: boolean | null }) {
  return (
    <>
      {editingOn === false && (
        <p className="modal__muted">
          Editing is <strong>off</strong> on this layer — desktop connections are read-only
          regardless.
        </p>
      )}
      {layer.shareLevel === 'organization' && (
        <p className="modal__muted">
          Shared to <strong>Organization</strong> — teammates connect the same way with their own
          tokens.
        </p>
      )}
      {!layer.canEdit && (
        <p className="modal__muted">You have view access only on this layer.</p>
      )}
    </>
  )
}

// --- desktop recipes: token → connection file → (manual + test collapsed) ---------

function DesktopRecipe({
  client,
  layer,
  authDoor,
  esriServiceUrl,
  esriLayerUrl,
  accessToken,
  myTokens,
  editingOn,
}: {
  client: 'pro' | 'qgis'
  layer: MyLayer
  authDoor: string
  esriServiceUrl: string
  esriLayerUrl: string
  accessToken: string
  myTokens: PatSummary[]
  editingOn: boolean | null
}) {
  const [minted, setMinted] = useState<PatMinted | null>(null)
  const [testOk, setTestOk] = useState(false)
  const current = minted === null ? 1 : testOk ? 4 : 2
  const app = client === 'pro' ? 'ArcGIS Pro' : 'QGIS'
  const host = authDoor
    .replace(/^https?:\/\//, '')
    .replace(/\/api\/v1\/ogc-auth$/, '')
    .replace(/[:/\\]/g, '_')

  function downloadFile(): void {
    if (client === 'qgis') {
      downloadTextFile(
        'talus-qgis-connection.xml',
        buildQgisConnectionsXml(`Talus GIS (${host})`, authDoor),
      )
    } else {
      downloadTextFile(
        `Talus GIS (${host}).ogc`,
        buildProOgcFile(`Talus GIS (${host})`, authDoor),
        'application/octet-stream',
      )
    }
  }

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">View in {app}</h3>
      <Gates layer={layer} editingOn={editingOn} />
      <p className="modal__muted">
        This layer is the collection <code className="sa-code">{layer.id}</code>.{' '}
        <strong>Already connected this computer?</strong> Then it’s already in your Talus
        connection — just expand it in {client === 'pro' ? 'Pro’s Catalog pane' : 'QGIS'} and add
        it. Setting up fresh:
      </p>

      <Step n={1} title="Access token" current={current}>
        <p className="ck__body">Create a token so {app} can authenticate with Talus.</p>
        <TokenStep
          accessToken={accessToken}
          myTokens={myTokens}
          minted={minted}
          setMinted={setMinted}
          editing={false}
          appName={app}
        />
      </Step>

      <Step n={2} title="Connection file" current={current}>
        <p className="ck__body">
          One download connects this computer to your whole viewable catalog — this layer and
          everything shared to you later.
        </p>
        <div className="ck__stack">
          <button type="button" className="btn" onClick={downloadFile}>
            Download connection file
          </button>
        </div>
        <Expandable label="Add JUST this layer instead (paste a link — like AGO)">
          <p className="modal__muted">
            One link, one layer — the AGO copy-link habit. Paste this into{' '}
            {client === 'pro' ? (
              <>
                Pro’s <strong>Add Data From Path</strong> (Map ribbon ▸ Add Data ▸ Data From Path)
              </>
            ) : (
              <>
                a QGIS <strong>ArcGIS REST Server</strong> connection (Data Source Manager ▸
                ArcGIS REST Server ▸ New, URL = the link), then expand the connection and
                double-click the layer
              </>
            )}{' '}
            and sign in with <code className="sa-code">talus</code> + your token. Read-only here;
            the Edit goal covers editing.
          </p>
          <CopyField value={client === 'pro' ? esriLayerUrl : esriServiceUrl} />
        </Expandable>
        <Expandable label="Set it up manually instead">
          <p className="modal__muted">Server URL (set by your administrator — read-only):</p>
          <CopyField value={authDoor} />
          {client === 'qgis' ? (
            <ol className="use-in__path">
              <li>
                Layer ▸ Data Source Manager ▸ <strong>WFS / OGC API — Features</strong> ▸ New.
              </li>
              <li>URL: the server URL above.</li>
              <li>
                Version: <strong>“OGC API — Features”</strong> — NOT the default “Maximum” (it
                probes classic WFS and fails with “error occurred while parsing element”).
              </li>
              <li>
                Authentication: <strong>Basic</strong> — username{' '}
                <code className="sa-code">talus</code>, password = your token.
              </li>
              <li>
                Leave <strong>“Max. number of features” blank</strong> — a value silently
                truncates the layer (sometimes with a “partially failed” banner).
              </li>
              <li>Connect → pick your layers from the collections list → Add.</li>
            </ol>
          ) : (
            <ol className="use-in__path">
              <li>
                Insert ▸ Connections ▸ Server ▸ <strong>New OGC API Server</strong>.
              </li>
              <li>Server URL: the server URL above.</li>
              <li>
                Authentication: <strong>Server Authentication</strong> — username{' '}
                <code className="sa-code">talus</code>, password = your token.
              </li>
              <li>Add the connection, then add layers from it in the Catalog pane.</li>
            </ol>
          )}
        </Expandable>
        {client === 'pro' && (
          <ProServerDialogNote
            route={
              <>
                Browse with <strong>New OGC API Server</strong> (or the connection file above); to
                edit, use this wizard’s <strong>Edit in ArcGIS Pro</strong> goal and paste the
                layer’s link.
              </>
            }
          />
        )}
      </Step>

      <Step n={3} title={`Add the layer in ${app}`} current={current}>
        {client === 'qgis' ? (
          <ol className="use-in__path">
            <li>
              Layer ▸ Data Source Manager ▸ WFS / OGC API — Features ▸ <strong>Load</strong>{' '}
              (next to Save, under the connections list) ▸ choose the file ▸{' '}
              <strong>Import</strong>. (The file isn’t a layer — opening it from the Browser
              panel does nothing.)
            </li>
            <li>
              <strong>Connect</strong>, and sign in with <code className="sa-code">talus</code> +
              your token.
            </li>
            <li>Pick your layers from the collections list → Add.</li>
          </ol>
        ) : (
          <ol className="use-in__path">
            <li>
              Save the <code className="sa-code">.ogc</code> into your project’s home folder (or
              any folder connection) — it appears in the Catalog pane.
            </li>
            <li>
              Open it and sign in with <code className="sa-code">talus</code> + your token.
            </li>
            <li>Add layers from it to your map.</li>
          </ol>
        )}
        {client === 'pro' && (
          <p className="modal__muted">
            <strong>Changed the URL or credential later?</strong> Fully restart Pro — it caches
            per session, and it keys stored credentials to the server URL (Windows Credential
            Manager).
          </p>
        )}
      </Step>

      <Step n={4} title="Verify" optional last current={current}>
        <p className="ck__body">Run a test to confirm your credentials can reach this layer.</p>
        <TestStep
          url={`${authDoor}/collections/${encodeURIComponent(layer.id)}`}
          externalToken={minted?.token ?? ''}
          requireToken
          onResult={setTestOk}
        />
      </Step>
    </section>
  )
}

// --- desktop EDITING (I-3, the GeoServices FeatureServer door) -----------------------

function EditRecipe({
  client,
  layer,
  esriServiceUrl,
  esriLayerUrl,
  esriCatalogUrl,
  chuteBase,
  accessToken,
  myTokens,
  editingOn,
  attachOn,
}: {
  client: 'pro' | 'qgis'
  layer: MyLayer
  esriServiceUrl: string
  esriLayerUrl: string
  esriCatalogUrl: string
  /** The QGIS write-chute door base (…/esri-auth/layers/{id}/attachments) — A-3. */
  chuteBase: string
  accessToken: string
  myTokens: PatSummary[]
  editingOn: boolean | null
  /** Layer attachments toggle (view-gated read); null = unknown. */
  attachOn: boolean | null
}) {
  const [minted, setMinted] = useState<PatMinted | null>(null)
  const [testOk, setTestOk] = useState(false)
  // "Need attachments?" (decision-attachments §2.3): configures the RECIPE, not the token —
  // read_write already gates attachment writes exactly as it gates edits. QGIS only.
  const [wantAttach, setWantAttach] = useState(false)
  const attachStep = client === 'qgis'
  const verifyN = attachStep ? 5 : 4
  const current = minted === null ? 1 : testOk ? verifyN : 2
  const app = client === 'pro' ? 'ArcGIS Pro' : 'QGIS'
  // The store-URL expression the Attachment widget evaluates per feature. Rig-proven shape
  // (decision §9): trailing '/' — QGIS itself appends the picked file's name on upload.
  const storeUrlExpr = `'${chuteBase}/' || "OBJECTID" || '/'`

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Edit in {app}</h3>

      {editingOn === false && (
        <p className="sa-warn">
          Editing is <strong>off</strong> on this layer — connections will be{' '}
          <strong>read-only</strong> until its owner turns editing on (Manage → Settings). The
          steps below still work for viewing.
        </p>
      )}
      {!layer.canEdit && (
        <p className="sa-warn">
          You have <strong>view access only</strong> on this layer — the server will refuse your
          edits (honestly, with an error) even with a Read &amp; edit token. Ask the owner for
          edit access first.
        </p>
      )}
      {layer.shareLevel === 'organization' && (
        <p className="modal__muted">
          Shared to <strong>Organization</strong> — teammates with edit access connect the same
          way with their own tokens; every edit is attributed to the person who made it.
        </p>
      )}

      <Step n={1} title="Read & edit token" current={current}>
        <p className="ck__body">Create a token so {app} can authenticate — and edit.</p>
        <TokenStep
          accessToken={accessToken}
          myTokens={myTokens}
          minted={minted}
          setMinted={setMinted}
          editing
          appName={app}
        />
      </Step>

      <Step n={2} title="This layer’s link" current={current}>
        <p className="ck__body">
          One link, one layer — paste it straight into {app}.
          {client === 'qgis' && ' (QGIS connections take the service link — no trailing /0.)'}
        </p>
        <CopyField value={client === 'pro' ? esriLayerUrl : esriServiceUrl} />
      </Step>

      <Step n={3} title={`Add & edit in ${app}`} current={current}>
        {client === 'pro' ? (
          <>
            <p className="sa-warn">
              <strong>Use Add Data From Path — NOT Insert ▸ Connections.</strong> The OGC API
              Server connection is a read-only path (Pro can’t edit OGC API Features) and it can’t
              read a FeatureServer URL — it fails with a “landing page failed” / timeout error. And
              Pro’s <strong>New ArcGIS Server Connection</strong> dialog fails outright against
              Talus (“unable to connect”, even with a valid token) — it requires Esri’s proprietary
              server-admin API. The steps below are the editable path.
            </p>
            <ol className="use-in__path">
              <li>
                Map ribbon ▸ Add Data (the drop-down) ▸ <strong>Data From Path</strong> → paste
                the link above → Add.
              </li>
              <li>
                Sign in when asked: username <code className="sa-code">talus</code>, password = the
                token. (Pro stores it in Windows Credential Manager, keyed to the server — to
                switch tokens later, clear it there and fully restart Pro.)
              </li>
              <li>
                Edit ribbon ▸ Create / Modify / Delete — edits save straight to Talus and appear
                in the portal (and its audit history) immediately.
              </li>
            </ol>
          </>
        ) : (
          <>
            <p className="sa-warn">
              <strong>Editing needs a saved authentication Configuration — the “Basic” tab won’t
              work.</strong>{' '}
              QGIS sends the Basic username/password only when <em>reading</em>; on an edit it
              sends only a Configuration. If you use the Basic tab, the layer draws fine and then
              edits fail with a confusing “read-only” error. Set up the Configuration in step 2
              below (or, if you already typed into Basic, click the dialog’s{' '}
              <strong>Convert to configuration</strong> button — one click).
            </p>
            <ol className="use-in__path">
              <li>
                Layer ▸ Data Source Manager ▸ <strong>ArcGIS REST Server</strong> ▸ New → Name it,
                URL = the link above.
              </li>
              <li>
                On the <strong>Configurations</strong> tab (not Basic), click <strong>＋</strong> →
                choose <strong>Basic authentication</strong> → username{' '}
                <code className="sa-code">talus</code>, password = the token → Save, then select it
                in the dropdown.
              </li>
              <li>
                OK to save → expand the connection in the Browser panel → double-click the layer
                to add it to the map.
              </li>
              <li>
                Toggle Editing (the pencil) → add / move / edit / delete → Save. Edits land in
                Talus and appear in the portal (and its audit history) immediately.
              </li>
            </ol>
          </>
        )}
        {client === 'qgis' && (
          <Expandable label="Connect ONCE to your whole catalog (recommended — set up per computer)">
            <p className="modal__muted">
              Instead of a per-layer link, point one connection at your catalog root and{' '}
              <strong>every layer you can see — including this one, and any shared to you later —
              appears under it</strong>, ready to expand and add. It’s the closest thing to “it’s
              just already there.” Same connection, same token; use this URL instead of the
              per-layer link in the New-connection dialog above:
            </p>
            <CopyField value={esriCatalogUrl} />
          </Expandable>
        )}
        {client === 'pro' && attachOn === true && (
          <p className="modal__muted">
            This layer has <strong>attachments</strong> enabled. Pro will show an extra field{' '}
            <code className="sa-code">_talus_attach</code> — it’s how QGIS uploads attachments;{' '}
            <strong>ignore it in Pro</strong> (edits to it are discarded). Pro-native attachment
            support arrives in a later release; until then, view and manage this layer’s
            attachments in the portal.
          </p>
        )}
      </Step>

      {attachStep && (
        <Step n={4} title="Attachments — photos & documents" optional current={current}>
          <label className="ck__body" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input
              type="checkbox"
              checked={wantAttach}
              onChange={(e) => setWantAttach(e.target.checked)}
            />
            Need attachments? Set up QGIS’s Attachment widget to upload photos/documents onto
            features.
          </label>
          {wantAttach && (
            <>
              {attachOn === false && (
                <p className="sa-warn">
                  Attachments are <strong>off</strong> on this layer — uploads will be refused
                  until its owner turns them on (Manage → Settings → Attachments). The setup below
                  still works; it starts working the moment the switch flips.
                </p>
              )}
              <p className="ck__body">
                Same token, same rules: your Read &amp; edit token uploads through the same gated,
                audited door as every other client. Files land on the feature and appear in the
                portal (and to other editors) immediately.
              </p>
              <ol className="use-in__path">
                <li>
                  Right-click the layer ▸ Properties ▸ <strong>Attributes Form</strong> ▸ select
                  the <code className="sa-code">_talus_attach</code> field.
                </li>
                <li>
                  Widget Type: <strong>Attachment</strong>.
                </li>
                <li>
                  Storage type: <strong>WebDAV Storage</strong> · Authentication = the{' '}
                  <strong>same configuration</strong> you created in step 2 above{' '}
                  <em>(desktop-only projects — see the note below for phones)</em>.
                </li>
                <li>
                  Store URL: click the <strong>ε</strong> (expression) button and paste:
                  <CopyField value={storeUrlExpr} />
                </li>
                <li>
                  Optional: Integrated document viewer ▸ Type = <strong>Image</strong> for a photo
                  preview in the form.
                </li>
                <li>OK to save the layer properties.</li>
              </ol>
              <p className="modal__muted">
                <strong>Uploading:</strong> the feature must be <strong>saved first</strong> (a
                brand-new feature gets its ID at save). Open its attribute form, click the file
                button next to the field, pick the file — <strong>it uploads the moment you pick
                it</strong> (allowed types: images, PDF, Office documents; 25 MB max). The field
                always shows the newest attachment; view or delete any of them in the portal’s
                record panel.
              </p>
              <p className="modal__muted">
                <strong>Taking this project to QField / a phone?</strong> An encrypted auth
                configuration does not travel. Set Authentication to{' '}
                <strong>No authentication</strong> and put your token inside the Store URL
                instead:{' '}
                <code className="sa-code">
                  {`'http://talus:<token>@…/attachments/' || "OBJECTID" || '/'`}
                </code>{' '}
                — the token then rides the project file in cleartext, so mint a disposable one
                per project and revoke it when the project retires. Note QField itself does not
                upload through this widget: field photos stay on the device and come back with
                the project folder for ingest (see the connect guide’s “Photos in the field”).
              </p>
            </>
          )}
        </Step>
      )}

      <Step n={verifyN} title="Verify" optional last current={current}>
        <p className="ck__body">Run a test to confirm your credentials can reach this layer.</p>
        <TestStep
          url={`${esriLayerUrl}?f=json`}
          externalToken={minted?.token ?? ''}
          requireToken
          onResult={setTestOk}
        />
      </Step>
    </section>
  )
}

// --- script / web app (the PUBLIC door) --------------------------------------------

function ScriptRecipe({
  layer,
  publicDoor,
  authDoor,
}: {
  layer: MyLayer
  publicDoor: string
  authDoor: string
}) {
  const isPublic = layer.shareLevel === 'public'
  const [testOk, setTestOk] = useState(false)
  const current = testOk ? 3 : 1
  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Use in a script or web app</h3>
      <p className="modal__muted">
        Standard OGC API — Features (plain JSON/GeoJSON): a landing page, <code className="sa-code">
        /collections</code>, and paged <code className="sa-code">/items</code>. Works with GDAL/OGR,
        Python (owslib, requests), or any HTTP client.
      </p>

      <Step n={1} title="Service URL" current={current}>
        <p className="ck__body">Anonymous / harvester (public layers only):</p>
        <CopyField value={`${publicDoor}/collections/${layer.id}/items`} />
        {!isPublic && (
          <p className="modal__muted">
            ⚠ This layer isn’t shared to Public Access — anonymous callers won’t see it there.
            Use the signed-in URL below, or share it publicly first.
          </p>
        )}
        <p className="ck__body" style={{ marginTop: '10px' }}>
          Signed-in (your full scope):
        </p>
        <CopyField value={`${authDoor}/collections/${layer.id}/items`} />
      </Step>

      <Step n={2} title="Authenticate" current={current}>
        <p className="ck__body">
          HTTP Basic: username <code className="sa-code">talus</code>, password = an access token
          — not your portal password (Profile → Access tokens).
        </p>
      </Step>

      <Step n={3} title="Verify" optional last current={current}>
        <p className="ck__body">Anonymous probe of the public door.</p>
        <TestStep
          url={`${publicDoor}/collections/${encodeURIComponent(layer.id)}`}
          externalToken=""
          requireToken={false}
          anonNote={
            isPublic
              ? undefined
              : 'expected to fail — the layer isn’t public; that’s the sharing model working'
          }
          onResult={setTestOk}
        />
      </Step>
    </section>
  )
}

// --- download ------------------------------------------------------------------------

function DownloadRecipe({
  layer,
  accessToken,
  isTable,
}: {
  layer: MyLayer
  accessToken: string
  isTable: boolean
}) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const formats: Array<{ fmt: 'gpkg' | 'geojson' | 'csv'; label: string; hint: string }> = [
    { fmt: 'gpkg', label: 'GeoPackage (.gpkg)', hint: 'best for desktop GIS — schema + CRS intact' },
    { fmt: 'geojson', label: 'GeoJSON', hint: 'web mapping and scripts' },
    { fmt: 'csv', label: 'CSV', hint: 'spreadsheets — attributes' },
  ]

  async function grab(fmt: 'gpkg' | 'geojson' | 'csv'): Promise<void> {
    setBusy(fmt)
    setError(null)
    try {
      const r = await downloadLayerExport(layer.id, {}, fmt, accessToken)
      if (r.truncated) {
        // Surfaced through the same channel as a failure — an incomplete file IS one
        // (decision-export-truncation.md).
        setError(
          `Export INCOMPLETE: ${r.returned.toLocaleString()} features — the server's per-export ` +
          `limit. Use the layer's table view to filter or select a subset and export again.`,
        )
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Export failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="modal__section">
      <h3 className="modal__section-title">Download a copy</h3>
      <p className="modal__muted">
        A snapshot of the {isTable ? 'table' : 'layer'} as of now — it won’t update when the data
        changes (connect live from a desktop app for that).
      </p>
      {(isTable ? formats.filter((f) => f.fmt === 'csv') : formats).map((f) => (
        <div key={f.fmt} className="use-in__credrow">
          <button
            type="button"
            className="btn btn--sm"
            disabled={busy !== null}
            onClick={() => void grab(f.fmt)}
          >
            {busy === f.fmt ? 'Preparing…' : f.label}
          </button>
          <span className="modal__muted">{f.hint}</span>
        </div>
      ))}
      {error && <p className="modal__field-error">{error}</p>}
    </section>
  )
}

// --- the catalog-row modal wrapper -------------------------------------------------------

export default function UseInWizard({
  layer,
  accessToken,
  onClose,
}: {
  layer: MyLayer
  accessToken: string
  onClose: () => void
}) {
  return (
    <div className="modal__backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Use this layer in desktop apps and scripts"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Use “{layer.displayName ?? layer.id}” in…</h2>
          <CloseButton onClick={onClose} />
        </div>
        <UseInContent
          layer={layer}
          accessToken={accessToken}
          isTable={layer.geomType === 'none'}
          onClose={onClose}
        />
      </div>
    </div>
  )
}
