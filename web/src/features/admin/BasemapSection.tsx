import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import {
  ApiError,
  fetchBasemapAdminStatus,
  setDefaultBasemap,
  setDefaultExtent,
  setDefaultUnits,
  setStreamedBasemaps,
  startBasemapFetch,
  uploadBasemapArchive,
  type BasemapAdminStatus,
  type BasemapJob,
  type DefaultBasemap,
  type MapUnits,
  type StreamedBasemap,
} from '@/lib/api'
import {
  BASEMAP_LABELS,
  DOWNLOADED_BASEMAPS,
  KIND_LABELS,
  STREAMED_BASEMAPS,
  downloadedListed,
  isStreamedBasemap,
  offeredLooks,
  offeredStreamed,
  pickerStreamed,
  plannedDefault,
} from '@/features/map/basemapNames'
import ExtentPicker from '@/features/map/ExtentPicker'
import { fieldBasemapNote } from '@paid'

/**
 * Admin ▸ Basemap. Three parts (decision-streamed-basemap.md §14.4, §15.10, §16):
 *
 * - WHAT THIS PORTAL LISTS — the choice between Streamed, Downloaded, or both (V116), which
 *   publisher-hosted maps it offers (V115), and the basemap a new map opens on (V093). A streamed
 *   map is fetched by each viewer's browser from its publisher, so offering one is an
 *   organization's decision about where its viewers' requests go. Downloaded is the setting for no
 *   external map-background request.
 * - MAP DEFAULTS — the org default extent and units (V094).
 * - DOWNLOADED MAP (decision-basemap-provisioning.md) — your town's map, day one: enter the
 *   jurisdiction bbox and Fetch (server-side go-pmtiles extract against the official Protomaps
 *   builds), or — air-gapped — upload a pre-extracted `.pmtiles`. Shows what's installed (build
 *   date, bbox, size, origin) and re-fetches to update.
 *
 * The server is the authority on every gate (admin-only writes, the closed list of names, bbox
 * caps, magic-bytes validation); this section is the friendly face. A basemap here is always a
 * NAME: nothing on this page takes, shows or sends a tile address.
 */

// County-scale soft warn (F7): above ~1 deg² the extract gets noticeably big — warn with a
// rough size hint but let it through; the server's hard cap (state-scale) is the real fence.
const SOFT_WARN_AREA_DEG2 = 1.0

const POLL_MS = 2500

// The streamed maps this BUILD can draw, in offered order: every known name, less imagery in a
// build that switched it off at build time (the rule lives in offeredStreamed, not here). These are
// the tick boxes. They come from the bundle's own closed list, never from an api answer.
const TICKABLE_STREAMED: readonly StreamedBasemap[] = offeredStreamed(STREAMED_BASEMAPS)

// What each streamed map is and who publishes it — the administrator is choosing where viewers'
// browsers go, so the publisher is named beside each tick box. Display text only.
const STREAMED_ABOUT: Record<StreamedBasemap, string> = {
  osm: 'Street map, worldwide, in three looks: standard, gray and dark. Published by OpenStreetMap.',
  usgstopo: 'Topographic street map, United States. Published by the U.S. Geological Survey.',
  imagery: 'Aerial imagery, United States. Published by the U.S. Geological Survey.',
}

function sameNames(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((v) => b.includes(v))
}

// What a portal lists (V116 / decision-streamed-basemap.md §16). Three states from two settings —
// whether the downloaded looks are listed, and which streamed publishers are offered. `both` is
// what every install had before V116 and stays a choice,
// so an administrator who tries another can return to it.
type ListMode = 'streamed' | 'downloaded' | 'both'

function modeOf(listsDownloaded: boolean, offered: readonly StreamedBasemap[]): ListMode {
  if (!listsDownloaded) return 'streamed'
  return offered.length > 0 ? 'both' : 'downloaded'
}

const MODE_CHOICES: { mode: ListMode; label: string; about: string }[] = [
  {
    mode: 'streamed',
    label: 'Streamed',
    about:
      'Maps come straight from their publisher over the internet. Nothing needs downloading. Light, Dark and Gray are not listed.',
  },
  {
    mode: 'downloaded',
    label: 'Downloaded',
    about:
      'Light, Dark and Gray, drawn from an area stored on this server. No viewer\u2019s browser is sent to the internet for a map background.',
  },
  {
    mode: 'both',
    label: 'Streamed and downloaded',
    about: 'Both kinds are listed, each under its own heading in the basemap list.',
  },
]

function parseNum(v: string): number | null {
  const n = Number(v.trim())
  return v.trim() !== '' && Number.isFinite(n) ? n : null
}

function fmtBytes(n: number | null): string {
  if (n === null) return '—'
  if (n >= 1024 * 1024 * 1024) return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`
  return `${Math.max(1, Math.round(n / (1024 * 1024)))} MB`
}

/** ~6-month staleness (the System-page nudge threshold, mirrored here). */
// eslint-disable-next-line react-refresh/only-export-components -- deliberate co-location (HealthView imports it); HMR full-reload accepted
export function basemapIsStale(status: { sourceDate: string | null; fetchedAt: string | null }): boolean {
  const ref = status.sourceDate
    ? new Date(`${status.sourceDate.slice(0, 4)}-${status.sourceDate.slice(4, 6)}-${status.sourceDate.slice(6, 8)}`)
    : status.fetchedAt
      ? new Date(status.fetchedAt)
      : null
  if (ref === null || Number.isNaN(ref.getTime())) return false
  return Date.now() - ref.getTime() > 183 * 24 * 3600 * 1000
}

export default function BasemapSection() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [status, setStatus] = useState<BasemapAdminStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [west, setWest] = useState('')
  const [south, setSouth] = useState('')
  const [east, setEast] = useState('')
  const [north, setNorth] = useState('')
  const [maxzoom, setMaxzoom] = useState('15')
  const [date, setDate] = useState('') // blank = latest
  const [busy, setBusy] = useState(false)
  const [savingDefault, setSavingDefault] = useState(false)
  // The choice and the tick boxes are a DRAFT, saved together: choosing Downloaded is the setting
  // for no external map request and should be one act (and one audit row). null = no pending edit,
  // the controls mirror what the portal lists. The mode is held explicitly, not derived from the
  // ticks, so choosing "Streamed" before ticking anything does not snap back. The ticks survive a
  // visit to Downloaded, where they are hidden and not sent. The 2.5 s job poll never touches it.
  const [listDraft, setListDraft] = useState<{ mode: ListMode; ticks: StreamedBasemap[] } | null>(
    null,
  )
  const [savingStreamed, setSavingStreamed] = useState(false)
  const [streamedSaved, setStreamedSaved] = useState(false)
  // Set when a save withdrew the map new maps opened on and the api reset that default (it does so
  // in the same write). Shown until dismissed — the select must not change without saying why.
  const [defaultReset, setDefaultReset] = useState<{ from: DefaultBasemap; to: DefaultBasemap } | null>(
    null,
  )
  // A fetch or an upload was seen to finish on this page. Drives the one-click offer to open new
  // maps on Light when the default is still a streamed map; nothing changes unless it is pressed.
  const [installSeen, setInstallSeen] = useState(false)
  const prevJobStateRef = useRef<BasemapJob['state'] | null>(null)
  // Map defaults (V094 / decision-org-default-extent.md): the org default extent inputs + units.
  // The extent fields mirror the STORED value (seeded below), unlike the fetch bbox above, which
  // is a one-shot form that starts blank.
  const [extW, setExtW] = useState('')
  const [extS, setExtS] = useState('')
  const [extE, setExtE] = useState('')
  const [extN, setExtN] = useState('')
  const [savingExtent, setSavingExtent] = useState(false)
  const [savingUnits, setSavingUnits] = useState(false)
  // Which bbox group has the inline map picker expanded (decision-extent-picker.md fork A) —
  // one at a time, so at most one extra MapLibre instance lives on the page.
  const [pickerFor, setPickerFor] = useState<'fetch' | 'extent' | null>(null)
  // Re-seed the inputs only when the SERVER value actually changes (tracked here), so the 2.5s
  // job poll can never clobber an admin mid-edit.
  const seededExtentRef = useRef<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async (): Promise<BasemapAdminStatus | null> => {
    if (!token) return null
    try {
      const s = await fetchBasemapAdminStatus(token)
      setStatus(s)
      setError(null)
      return s
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load basemap status')
      return null
    }
  }, [token])

  // Load on mount; poll while a job runs so Fetch progress and completion show live.
  useEffect(() => {
    void refresh()
  }, [refresh])
  useEffect(() => {
    if (status?.job.state !== 'running') return
    pollRef.current = window.setInterval(() => void refresh(), POLL_MS)
    return () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
    }
  }, [status?.job.state, refresh])

  const bbox = [parseNum(west), parseNum(south), parseNum(east), parseNum(north)]
  const bboxComplete = bbox.every((v) => v !== null)
  const [w, s, e, n] = bbox as [number, number, number, number]
  const bboxInvalid =
    bboxComplete && !(w < e && s < n && w >= -180 && e <= 180 && s >= -85.06 && n <= 85.06)
  const area = bboxComplete && !bboxInvalid ? (e - w) * (n - s) : 0
  const zoomNum = parseNum(maxzoom)
  const zoomInvalid = zoomNum === null || zoomNum < 1 || zoomNum > 15 || !Number.isInteger(zoomNum)
  const dateTrimmed = date.trim()
  const dateInvalid = dateTrimmed !== '' && !/^\d{8}$/.test(dateTrimmed)
  const running = status?.job.state === 'running'
  // Map-defaults extent derivations — same client-side gate as the fetch bbox above; the server's
  // shared validator (V094) is the authority.
  const extVals = [parseNum(extW), parseNum(extS), parseNum(extE), parseNum(extN)]
  const extComplete = extVals.every((v) => v !== null)
  const [ew, es, ee, en] = extVals as [number, number, number, number]
  const extInvalid =
    extComplete && !(ew < ee && es < en && ew >= -180 && ee <= 180 && es >= -85.06 && en <= 85.06)
  const storedExtent = status?.defaultExtent ?? null
  // What this portal offers, through the one shared reader. EMPTY until the admin status has
  // loaded (and if it never loads), so nothing on this page reaches a publisher on a guess — the
  // same fail-closed rule as the map (decision-streamed-basemap.md §15.9, finding 1).
  const offered: StreamedBasemap[] = status !== null ? offeredStreamed(status.streamed) : []
  // Whether the downloaded looks are listed, as saved (V116). Listed until the status says not.
  const listed = status !== null ? downloadedListed(status.downloadedListed) : true
  const savedMode = modeOf(listed, offered)
  const mode = listDraft?.mode ?? savedMode
  const ticked = listDraft?.ticks ?? offered
  // What a save would send: Downloaded sends no publisher whatever is ticked behind it.
  const sendValue: StreamedBasemap[] = mode === 'downloaded' ? [] : ticked
  const sendDownloaded = mode !== 'streamed'
  // A portal must list something: Streamed, or both, with nothing ticked cannot be saved.
  const listValid = mode === 'downloaded' || ticked.length > 0
  const listDirty =
    listDraft !== null && (sendDownloaded !== listed || !sameNames(sendValue, offered))
  const currentDefault = status?.defaultBasemap ?? 'light'
  // What "new maps open on" would become: when its map stops being listed the api moves it in the
  // same write. Worded here before the save; the api's answer is what is adopted afterwards.
  const defaultAfterSave = listValid ? plannedDefault(currentDefault, sendDownloaded, sendValue) : null
  const saveMovesDefault =
    listDirty && defaultAfterSave !== null && defaultAfterSave !== currentDefault
  // "New maps open on": only what the portal lists AS SAVED (the api answers 409 otherwise), in
  // the same two groups as the map's basemap list. The stored value is kept in the list even if
  // this build cannot draw it, so the select never shows a value that is not the stored one.
  const defaultDownloaded: DefaultBasemap[] = listed ? [...DOWNLOADED_BASEMAPS] : []
  const defaultStreamed: DefaultBasemap[] = offeredLooks(offered)
  if (!defaultDownloaded.includes(currentDefault) && !defaultStreamed.includes(currentDefault))
    (isStreamedBasemap(currentDefault) ? defaultStreamed : defaultDownloaded).push(currentDefault)
  // The picker exists wherever its map can draw: the downloaded area, or — with none — a streamed
  // map the organization OFFERS (before V115 this was the build-time imagery flag, which ignored
  // the offered list: security review finding 2). No area and nothing offered is the one row with
  // no drawable surface (decision-extent-picker.md fork C) — the buttons vanish and the page says so.
  const pickerOn = (status?.installed ?? false) ? null : pickerStreamed(offered)
  const pickerAvailable = (status?.installed ?? false) || pickerOn !== null

  function applyFetchBbox(b: [number, number, number, number] | null): void {
    setWest(b !== null ? String(b[0]) : '')
    setSouth(b !== null ? String(b[1]) : '')
    setEast(b !== null ? String(b[2]) : '')
    setNorth(b !== null ? String(b[3]) : '')
  }

  function applyExtentBbox(b: [number, number, number, number] | null): void {
    setExtW(b !== null ? String(b[0]) : '')
    setExtS(b !== null ? String(b[1]) : '')
    setExtE(b !== null ? String(b[2]) : '')
    setExtN(b !== null ? String(b[3]) : '')
  }

  async function changeDefaultBasemap(value: DefaultBasemap): Promise<void> {
    setSavingDefault(true)
    setError(null)
    try {
      await setDefaultBasemap(value, token)
      // Patch in place rather than re-fetching: a full refresh would also restart the job poll.
      setStatus((prev) => (prev ? { ...prev, defaultBasemap: value } : prev))
      setDefaultReset(null) // the administrator has now chosen; the reset message is history
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not change the default basemap')
    } finally {
      setSavingDefault(false)
    }
  }

  function chooseMode(next: ListMode): void {
    setListDraft({ mode: next, ticks: ticked })
    setStreamedSaved(false)
  }

  function toggleStreamed(name: StreamedBasemap, on: boolean): void {
    const next = TICKABLE_STREAMED.filter((b) => (b === name ? on : ticked.includes(b)))
    setListDraft({ mode, ticks: next })
    setStreamedSaved(false)
  }

  async function saveStreamed(): Promise<void> {
    if (listDraft === null || status === null || !listValid) return
    const before = status.defaultBasemap
    setSavingStreamed(true)
    setError(null)
    try {
      const res = await setStreamedBasemaps(sendValue, sendDownloaded, token)
      // Adopt the api's answer for ALL THREE values (patched in place, the changeDefaultBasemap
      // reasoning). If it moved the default, say so rather than let the select move on its own.
      setStatus((prev) =>
        prev
          ? {
              ...prev,
              streamed: res.streamed,
              downloadedListed: res.downloadedListed,
              defaultBasemap: res.defaultBasemap,
            }
          : prev,
      )
      setListDraft(null)
      setStreamedSaved(true)
      setDefaultReset(res.defaultBasemap !== before ? { from: before, to: res.defaultBasemap } : null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save what this portal lists')
    } finally {
      setSavingStreamed(false)
    }
  }

  // A fetch seen to go from running to done on this page (the upload path sets the flag itself).
  useEffect(() => {
    const now = status?.job.state ?? null
    if (prevJobStateRef.current === 'running' && now === 'done') setInstallSeen(true)
    prevJobStateRef.current = now
  }, [status?.job.state])

  useEffect(() => {
    if (status === null) return
    const key = JSON.stringify(status.defaultExtent)
    if (seededExtentRef.current === key) return
    seededExtentRef.current = key
    const ext = status.defaultExtent
    setExtW(ext ? String(ext[0]) : '')
    setExtS(ext ? String(ext[1]) : '')
    setExtE(ext ? String(ext[2]) : '')
    setExtN(ext ? String(ext[3]) : '')
  }, [status])

  async function saveDefaultExtent(value: [number, number, number, number] | null): Promise<void> {
    setSavingExtent(true)
    setError(null)
    try {
      await setDefaultExtent(value, token)
      // Patch in place rather than re-fetching (the changeDefaultBasemap reasoning): a full
      // refresh would also restart the job poll. The seed effect then re-syncs the inputs.
      setStatus((prev) => (prev ? { ...prev, defaultExtent: value } : prev))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not save the default extent')
    } finally {
      setSavingExtent(false)
    }
  }

  async function changeDefaultUnits(value: MapUnits): Promise<void> {
    setSavingUnits(true)
    setError(null)
    try {
      await setDefaultUnits(value, token)
      setStatus((prev) => (prev ? { ...prev, defaultUnits: value } : prev))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not change the units default')
    } finally {
      setSavingUnits(false)
    }
  }

  function readLastViewport(): [string, string, string, string] | null {
    let raw: string | null
    try {
      raw = localStorage.getItem('talus:lastMapViewport')
    } catch {
      raw = null
    }
    if (!raw) return null
    const parts = raw.split(',')
    return parts.length === 4 ? (parts as [string, string, string, string]) : null
  }

  function grabLastViewport(): void {
    const parts = readLastViewport()
    if (!parts) return
    setWest(parts[0])
    setSouth(parts[1])
    setEast(parts[2])
    setNorth(parts[3])
  }

  function grabLastViewportForExtent(): void {
    const parts = readLastViewport()
    if (!parts) return
    setExtW(parts[0])
    setExtS(parts[1])
    setExtE(parts[2])
    setExtN(parts[3])
  }

  function grabCoverageForExtent(): void {
    const b = status?.bbox
    if (!b) return
    setExtW(String(b[0]))
    setExtS(String(b[1]))
    setExtE(String(b[2]))
    setExtN(String(b[3]))
  }

  const hasViewport = ((): boolean => {
    try {
      return localStorage.getItem('talus:lastMapViewport') !== null
    } catch {
      return false
    }
  })()

  async function doFetch(): Promise<void> {
    if (!bboxComplete || bboxInvalid || zoomInvalid || dateInvalid || running) return
    setBusy(true)
    setError(null)
    try {
      await startBasemapFetch(
        {
          bbox: [w, s, e, n],
          maxzoom: zoomNum as number,
          date: dateTrimmed === '' ? 'latest' : dateTrimmed,
        },
        token,
      )
      await refresh() // flips job.state to running -> the poll effect takes over
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Fetch failed to start')
    } finally {
      setBusy(false)
    }
  }

  async function doUpload(file: File): Promise<void> {
    setBusy(true)
    setError(null)
    try {
      await uploadBasemapArchive(file, token)
      // Re-read the full status rather than adopting the upload's own answer: that answer is the
      // metadata WITHOUT the job block, and this page reads status.job on every render, so
      // storing it as the status broke the page after a successful upload.
      const fresh = await refresh()
      if (fresh?.installed) setInstallSeen(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Upload failed')
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  // Heading + intro live on BasemapView (handoff-admin-1a §3.5) — the section is the body of
  // its own routed destination now, not a card inside Settings.
  return (
    <section className="org-settings__card">

      {error !== null && <p className="sa__error">{error}</p>}

      {status === null ? (
        <p className="sa__muted">Loading…</p>
      ) : (
        <>
          {/* WHAT THIS PORTAL LISTS (V115, V116 / decision-streamed-basemap.md §14.4, §15.10, §16).
              The wording here is the control against an administrator making this choice without
              knowing what it sends where — change it only after review. */}
          <h2 className="org-settings__heading">What this portal lists</h2>
          <p className="basemap__help">
            A streamed map is not stored on this server. Each viewer&rsquo;s browser fetches it
            directly from its publisher while they look at a map, and that includes signed-out
            visitors to public maps and embedded maps. The publisher sees the viewer&rsquo;s
            internet address and the area being viewed. A downloaded map is stored here and sends
            no viewer anywhere.
          </p>
          <fieldset className="basemap__streamed" disabled={savingStreamed}>
            <legend className="basemap__streamed-legend">Basemaps in the list on every map</legend>
            {MODE_CHOICES.map((c) => (
              <label key={c.mode} className="basemap__streamed-row">
                <input
                  type="radio"
                  name="basemap-list-mode"
                  checked={mode === c.mode}
                  onChange={() => chooseMode(c.mode)}
                />
                <span>
                  <span className="basemap__streamed-name">{c.label}</span>
                  <span className="basemap__streamed-about">{c.about}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {/* Talus Field's qualifier, from the '@paid' seam (null in a Community build): a phone that
              cannot reach this server cannot learn the choice above changed (§19, H1). */}
          {fieldBasemapNote() && <p className="basemap__help">{fieldBasemapNote()}</p>}
          {mode !== 'downloaded' && (
            <fieldset className="basemap__streamed basemap__streamed--sub" disabled={savingStreamed}>
              <legend className="basemap__streamed-legend">Streamed maps this portal offers</legend>
              {TICKABLE_STREAMED.map((name) => (
                <label key={name} className="basemap__streamed-row">
                  <input
                    type="checkbox"
                    checked={ticked.includes(name)}
                    onChange={(ev) => toggleStreamed(name, ev.target.checked)}
                  />
                  <span>
                    <span className="basemap__streamed-name">{BASEMAP_LABELS[name]}</span>
                    <span className="basemap__streamed-about">{STREAMED_ABOUT[name]}</span>
                  </span>
                </label>
              ))}
            </fieldset>
          )}
          {!listValid && (
            <p className="sa-warn" role="status">
              Tick at least one streamed map, or choose Downloaded.
            </p>
          )}
          {mode === 'downloaded' && !status.installed && (
            <p className="sa-warn" role="status">
              No area is downloaded yet. Until one is, maps have no background. The download
              controls are under &ldquo;Downloaded map&rdquo; below.
            </p>
          )}
          <p className="basemap__help">
            A saved map on a streamed map this portal stops offering opens on what new maps open
            on. A saved map on Light, Dark or Gray keeps its look for as long as an area is
            downloaded.
          </p>
          {saveMovesDefault && defaultAfterSave !== null && (
            <p className="sa-warn" role="status">
              New maps open on {BASEMAP_LABELS[currentDefault]} today. Saving this will change that
              to {BASEMAP_LABELS[defaultAfterSave]}.
            </p>
          )}
          <div className="basemap__streamed-actions">
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void saveStreamed()}
              disabled={!listDirty || !listValid || savingStreamed}
            >
              Save
            </button>
            {listDraft !== null && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setListDraft(null)}
                disabled={savingStreamed}
              >
                Undo changes
              </button>
            )}
            {streamedSaved && listDraft === null && (
              <span className="org-settings__saved" role="status">
                Saved
              </span>
            )}
          </div>
          {defaultReset !== null && (
            <div className="callout callout--warning basemap__notice" role="status">
              <p>
                {BASEMAP_LABELS[defaultReset.from]} is no longer listed, so new maps now open on{' '}
                {BASEMAP_LABELS[defaultReset.to]}. You can choose another below.
              </p>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setDefaultReset(null)}
              >
                Dismiss
              </button>
            </div>
          )}

          <div className="basemap__default">
            <label className="basemap__default-field">
              <span>New maps open on</span>
              <select
                value={status.defaultBasemap}
                disabled={savingDefault || savingStreamed}
                onChange={(ev) => void changeDefaultBasemap(ev.target.value as DefaultBasemap)}
              >
                {defaultDownloaded.length > 0 && (
                  <optgroup label={KIND_LABELS.downloaded}>
                    {defaultDownloaded.map((key) => (
                      <option key={key} value={key}>
                        {BASEMAP_LABELS[key]}
                      </option>
                    ))}
                  </optgroup>
                )}
                {defaultStreamed.length > 0 && (
                  <optgroup label={KIND_LABELS.streamed}>
                    {defaultStreamed.map((key) => (
                      <option key={key} value={key}>
                        {BASEMAP_LABELS[key]}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
            <p className="sa__muted">
              The basemap a new map opens on, from the ones this portal lists as saved above. Saved
              maps keep the basemap they were saved with.
            </p>
          </div>

          {/* Map defaults (V094 / decision-org-default-extent.md): extent + units, below the
              basemap select (fork D). The extent inputs mirror the stored value; blank = unset. */}
          <h2 className="org-settings__heading">Map defaults</h2>
          <div className="basemap__default">
            <span className="basemap__default-field">Default extent</span>
            <div className="basemap__bbox">
              <label>
                West
                <input type="number" step="any" value={extW} onChange={(ev) => setExtW(ev.target.value)} />
              </label>
              <label>
                South
                <input type="number" step="any" value={extS} onChange={(ev) => setExtS(ev.target.value)} />
              </label>
              <label>
                East
                <input type="number" step="any" value={extE} onChange={(ev) => setExtE(ev.target.value)} />
              </label>
              <label>
                North
                <input type="number" step="any" value={extN} onChange={(ev) => setExtN(ev.target.value)} />
              </label>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={grabLastViewportForExtent}
                disabled={!hasViewport}
                title={
                  hasViewport
                    ? 'Fill from the map view you last looked at'
                    : 'Open the Map and pan to your jurisdiction first'
                }
              >
                Use last map view
              </button>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={grabCoverageForExtent}
                disabled={!status.bbox}
                title={
                  status.bbox
                    ? 'Fill from the installed basemap coverage'
                    : 'No basemap installed yet'
                }
              >
                Use basemap coverage
              </button>
              {pickerAvailable && (
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => setPickerFor(pickerFor === 'extent' ? null : 'extent')}
                >
                  {pickerFor === 'extent' ? 'Hide map' : 'Set on map'}
                </button>
              )}
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => void saveDefaultExtent([ew, es, ee, en])}
                disabled={!extComplete || extInvalid || savingExtent}
              >
                Save extent
              </button>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => void saveDefaultExtent(null)}
                disabled={storedExtent === null || savingExtent}
              >
                Clear
              </button>
            </div>
            {extInvalid && (
              <p className="sa__error">
                Bounds must satisfy west &lt; east and south &lt; north, in WGS84 degrees.
              </p>
            )}
            <p className="sa__muted">
              Where a new map opens. Saved maps keep their own view. Seeded automatically from the
              basemap coverage the first time one is provisioned; unset falls back to fitting the
              basemap coverage.
            </p>
            {pickerFor === 'extent' && pickerAvailable && (
              <ExtentPicker
                value={extComplete && !extInvalid ? [ew, es, ee, en] : null}
                onChange={applyExtentBbox}
                accessToken={token !== '' ? token : undefined}
                installed={status.installed}
                streamed={pickerOn}
                coverageBbox={status.bbox}
                onClose={() => setPickerFor(null)}
              />
            )}
          </div>

          <div className="basemap__default">
            <label className="basemap__default-field">
              <span>Default units</span>
              <select
                value={status.defaultUnits}
                disabled={savingUnits}
                onChange={(ev) => void changeDefaultUnits(ev.target.value as MapUnits)}
              >
                <option value="metric">Metric</option>
                <option value="imperial">US Standard</option>
              </select>
            </label>
            <p className="sa__muted">Used by the scale bar on embedded maps.</p>
          </div>

          {/* DOWNLOADED MAP (decision-basemap-provisioning.md) — Fetch and Upload, as before V115. */}
          <h2 className="org-settings__heading">Downloaded map</h2>
          <p className="basemap__help">
            Download your area once and Light, Dark and Gray draw from this server, with no
            internet connection needed by viewers. Fetch it from the latest OpenStreetMap-derived
            Protomaps build, or upload a pre-extracted <code>.pmtiles</code> file on an air-gapped
            network.
          </p>
          {installSeen && status.installed && !listed && (
            <div className="callout callout--success basemap__notice" role="status">
              <p>
                The area is downloaded. This portal lists streamed maps only, so Light, Dark and
                Gray are not in the basemap list. To list them, choose Downloaded or Streamed and
                downloaded above.
              </p>
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setInstallSeen(false)}
              >
                Dismiss
              </button>
            </div>
          )}
          {installSeen && status.installed && listed && isStreamedBasemap(status.defaultBasemap) && (
            <div className="callout callout--success basemap__notice" role="status">
              <p>
                The area is downloaded. New maps still open on{' '}
                {BASEMAP_LABELS[status.defaultBasemap]}, a streamed map.
              </p>
              <div className="basemap__notice-actions">
                <button
                  type="button"
                  className="btn btn--sm"
                  disabled={savingDefault}
                  onClick={() => {
                    setInstallSeen(false)
                    void changeDefaultBasemap('light')
                  }}
                >
                  Open new maps on Light
                </button>
                <button
                  type="button"
                  className="btn btn--sm btn--ghost"
                  onClick={() => setInstallSeen(false)}
                >
                  Keep {BASEMAP_LABELS[status.defaultBasemap]}
                </button>
              </div>
            </div>
          )}
          <p>
            {status.installed ? (
              <>
                Installed: {status.origin === 'uploaded' ? 'uploaded archive' : `build ${status.sourceDate}`}
                {' · '}
                {fmtBytes(status.sizeBytes)}
                {status.maxzoom !== null && <> · to zoom {status.maxzoom}</>}
                {status.fetchedAt !== null && (
                  <> · installed {new Date(status.fetchedAt).toLocaleDateString()}</>
                )}
                {basemapIsStale(status) && (
                  <span className="basemap__stale"> · over 6 months old — consider updating</span>
                )}
              </>
            ) : (
              <>No basemap installed yet — maps show a notice until one is provisioned.</>
            )}
          </p>

          {running && (
            <p className="basemap__job" role="status">
              Working: {status.job.message ?? 'provisioning…'} (this can take a few minutes)
            </p>
          )}
          {status.job.state === 'failed' && (
            <p className="sa__error">Last operation failed: {status.job.message}</p>
          )}

          <div className="basemap__bbox">
            <label>
              West
              <input type="number" step="any" value={west} onChange={(ev) => setWest(ev.target.value)} />
            </label>
            <label>
              South
              <input type="number" step="any" value={south} onChange={(ev) => setSouth(ev.target.value)} />
            </label>
            <label>
              East
              <input type="number" step="any" value={east} onChange={(ev) => setEast(ev.target.value)} />
            </label>
            <label>
              North
              <input type="number" step="any" value={north} onChange={(ev) => setNorth(ev.target.value)} />
            </label>
            <button
              type="button"
              className="btn btn--sm btn--ghost"
              onClick={grabLastViewport}
              disabled={!hasViewport}
              title={
                hasViewport
                  ? 'Fill from the map view you last looked at'
                  : 'Open the Map and pan to your jurisdiction first'
              }
            >
              Use last map view
            </button>
            {pickerAvailable && (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => setPickerFor(pickerFor === 'fetch' ? null : 'fetch')}
              >
                {pickerFor === 'fetch' ? 'Hide map' : 'Set on map'}
              </button>
            )}
          </div>
          {!pickerAvailable && (
            <p className="sa__muted">
              There is no map to pick the area on: no area is downloaded yet and no streamed map
              is offered. Type the bounds above, or tick a streamed map and save. On an air-gapped
              network, upload a packaged archive (see the provisioning runbook); the map picker
              then works from it.
            </p>
          )}
          {bboxInvalid && (
            <p className="sa__error">
              Bounds must satisfy west &lt; east and south &lt; north, in WGS84 degrees.
            </p>
          )}
          {!bboxInvalid && area > SOFT_WARN_AREA_DEG2 && (
            <p className="basemap__warn">
              That's a large area ({area.toFixed(1)} deg²) — the extract may take a while and weigh
              in at hundreds of MB. County-scale or smaller keeps it snappy.
            </p>
          )}
          {pickerFor === 'fetch' && pickerAvailable && (
            <ExtentPicker
              value={bboxComplete && !bboxInvalid ? [w, s, e, n] : null}
              onChange={applyFetchBbox}
              accessToken={token !== '' ? token : undefined}
              installed={status.installed}
              streamed={pickerOn}
              coverageBbox={status.bbox}
              onClose={() => setPickerFor(null)}
            />
          )}

          <div className="basemap__opts">
            <label>
              Max zoom
              <input
                type="number"
                min={1}
                max={15}
                value={maxzoom}
                onChange={(ev) => setMaxzoom(ev.target.value)}
              />
            </label>
            <label>
              Build date
              <input
                type="text"
                placeholder="latest"
                value={date}
                onChange={(ev) => setDate(ev.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void doFetch()}
              disabled={!bboxComplete || bboxInvalid || zoomInvalid || dateInvalid || busy || running}
            >
              {status.installed ? 'Update basemap' : 'Fetch basemap'}
            </button>
          </div>
          {zoomInvalid && maxzoom.trim() !== '' && (
            <p className="sa__error">Max zoom must be a whole number from 1 to 15.</p>
          )}
          {dateInvalid && <p className="sa__error">Build date must be YYYYMMDD (or blank for latest).</p>}

          <div className="basemap__upload">
            <label className="btn btn--sm btn--ghost">
              Upload .pmtiles…
              <input
                ref={fileRef}
                type="file"
                accept=".pmtiles"
                style={{ display: 'none' }}
                disabled={busy || running}
                onChange={(ev) => {
                  const f = ev.target.files?.[0]
                  if (f) void doUpload(f)
                }}
              />
            </label>
            <span className="sa__muted">
              Air-gapped path: extract on any connected machine, then walk the file in (see the
              provisioning runbook).
            </span>
          </div>
        </>
      )}
    </section>
  )
}
