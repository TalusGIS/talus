import { useEffect, useState } from 'react'
import { useAuth } from 'react-oidc-context'

import ConfirmDialog from '@/components/ConfirmDialog'
import {
  ApiError,
  fetchOrgSettings,
  fetchPublicSharing,
  updateOrgSettings,
  updatePublicSharing,
  type OrgSettings,
  type PublicSharingState,
} from '@/lib/api'

/**
 * Admin → Sharing & access (handoff-admin-1a §3.2, split out of the Settings catch-all).
 * Two concerns, both about who may expose what, with two deliberately different save models:
 *
 * The MASTER switch (decision-public-sharing-reachable.md) — whether this organization
 * offers public access at all. Its own endpoint, committing the moment it is confirmed and
 * never batched behind a Save button: enabling internet-facing publication is a separate
 * deliberate act, and turning it OFF stops serving already-public items immediately.
 *
 * The POLICY checkboxes (Talus 23/26) — may non-admin owners share publicly / transfer
 * ownership themselves. Batched under one "Save policy" button via PUT /org-settings. The
 * PUT body requires the banner fields, so the save echoes the values loaded here — the
 * governance keys are what this page owns; Notices owns the banner.
 *
 * Everything admin-gated server-side; behind RequireAdmin.
 */
export default function SharingView() {
  const auth = useAuth()
  const token = auth.user?.access_token ?? ''

  const [saved, setSaved] = useState<OrgSettings | null>(null)
  const [nonadminPublic, setNonadminPublic] = useState(false)
  const [nonadminTransfer, setNonadminTransfer] = useState(true)
  const [publicSharing, setPublicSharing] = useState<PublicSharingState | null>(null)
  const [sharingBusy, setSharingBusy] = useState(false)
  const [sharingError, setSharingError] = useState<string | null>(null)
  // The master switch's pending direction while its ConfirmDialog is open (null = closed).
  const [confirmNext, setConfirmNext] = useState<boolean | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [justSaved, setJustSaved] = useState(false)

  useEffect(() => {
    if (!token) return
    let active = true
    fetchOrgSettings(token)
      .then((s) => {
        if (!active) return
        setSaved(s)
        setNonadminPublic(s.nonadminCanSharePublic)
        setNonadminTransfer(s.nonadminCanTransferOwnership)
        setError(null)
      })
      .catch((e) => active && setError(e instanceof ApiError ? e.message : 'Failed to load settings'))
    // Its own endpoint, so its own fetch. Failure here must not blank the page — an admin
    // who cannot read the master switch can still set the policy.
    fetchPublicSharing(token)
      .then((ps) => active && setPublicSharing(ps))
      .catch(
        (e) =>
          active &&
          setSharingError(e instanceof ApiError ? e.message : 'Failed to load public sharing'),
      )
    return () => {
      active = false
    }
  }, [token])

  /**
   * Flip the master switch (after its ConfirmDialog is confirmed). Both directions confirm,
   * for different reasons.
   *
   * ON is the blunt one: this is the moment a municipal deployment starts publishing to the open
   * internet, and it should never happen by a stray click on a checkbox.
   *
   * OFF names the consequence with a number, because the switch gates the anonymous READ path and
   * not merely the write path: items that are already public STOP BEING SERVED the instant it
   * flips. That is the right behaviour — the alternative leaves data exposed after an admin
   * believes they closed the door — but it must be stated, not discovered.
   */
  async function toggleSharing(next: boolean): Promise<void> {
    setSharingBusy(true)
    setSharingError(null)
    try {
      setPublicSharing(await updatePublicSharing(next, token))
      // The share dialogs read the master switch out of the org-settings projection, so re-read it
      // here — otherwise this page and the rest of the portal would disagree until a reload.
      setSaved(await fetchOrgSettings(token))
    } catch (e) {
      setSharingError(e instanceof ApiError ? e.message : 'Could not change public sharing')
    } finally {
      setSharingBusy(false)
      setConfirmNext(null)
    }
  }

  /** The master switch's confirmation copy — the well-written browser-alert prompt, re-housed (§5). */
  function sharingConfirm(next: boolean) {
    const n = publicSharing?.publicItemCount ?? null
    if (next) {
      return (
        <ConfirmDialog
          title="Turn on public sharing?"
          confirmLabel="Turn on public sharing"
          busy={sharingBusy}
          onConfirm={() => void toggleSharing(true)}
          onCancel={() => setConfirmNext(null)}
          consequence="Turn this on only if this server is meant to be reachable by the public."
        >
          <p>
            Layers, maps and Open Data datasets shared to Public Access become readable by{' '}
            <strong>anyone on the internet</strong>, with no sign-in. Nothing becomes public on
            its own — you choose what to share — but the capability is live from the moment you
            confirm.
          </p>
        </ConfirmDialog>
      )
    }
    return (
      <ConfirmDialog
        title="Turn off public sharing?"
        confirmLabel="Turn off public sharing"
        busy={sharingBusy}
        onConfirm={() => void toggleSharing(false)}
        onCancel={() => setConfirmNext(null)}
        consequence={
          n === 0 ? undefined : (
            <>
              {n === null
                ? 'Anything currently shared publicly (the number could not be read just now) stops being served'
                : `${n} item${n === 1 ? ' is' : 's are'} currently shared publicly. Turning this off stops serving ${n === 1 ? 'it' : 'them'}`}{' '}
              immediately — public maps, embedded maps, and the Open Data / DCAT-US feed all go
              dark right away.
            </>
          )
        }
      >
        {n === 0 ? (
          <p>
            Nothing is shared publicly right now, so nothing stops being served. Public sharing,
            the public map embed and the Open Data feed become unavailable until you turn this
            back on.
          </p>
        ) : (
          <p>
            Map tiles a visitor’s browser has already fetched may linger for up to a minute.
            Each item keeps its sharing setting, so turning this back on restores{' '}
            {n === 1 ? 'it' : 'them'}.
          </p>
        )}
      </ConfirmDialog>
    )
  }

  const dirty =
    saved !== null &&
    (nonadminPublic !== saved.nonadminCanSharePublic ||
      nonadminTransfer !== saved.nonadminCanTransferOwnership)

  function edit(fn: () => void): void {
    fn()
    setJustSaved(false)
  }

  async function savePolicy(): Promise<void> {
    if (saved === null) return
    setBusy(true)
    setError(null)
    try {
      // The PUT body requires the banner fields — echo what this page loaded so a policy
      // save never changes the notice (Notices owns those fields).
      const next = await updateOrgSettings(
        {
          bannerEnabled: saved.bannerEnabled,
          bannerMessage: saved.bannerMessage,
          bannerLevel: saved.bannerLevel,
          nonadminCanSharePublic: nonadminPublic,
          nonadminCanTransferOwnership: nonadminTransfer,
        },
        token,
      )
      setSaved(next)
      setNonadminPublic(next.nonadminCanSharePublic)
      setNonadminTransfer(next.nonadminCanTransferOwnership)
      setJustSaved(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="admin">
      <h1 className="admin__title">Sharing &amp; access</h1>
      <p className="admin__note">
        Who may expose what: whether this organization publishes to the open internet at all,
        and what non-admin owners may do on their own.
      </p>

      <section className="org-settings__card">
        <h2 className="org-settings__heading">Public access</h2>
        <p className="admin__note">
          Whether this organization publishes anything to the open internet. This one switch
          governs three things: sharing a layer or map to <strong>Public Access</strong>,{' '}
          <strong>embedding</strong> a public map in another website, and the{' '}
          <strong>Open Data</strong> catalog and its DCAT-US feed (what a site like data.gov
          harvests). While it is off, all three are unavailable — nothing is served to anonymous
          visitors, whatever an item&rsquo;s own sharing says.
        </p>

        {sharingError !== null && <p className="sa__error">{sharingError}</p>}

        {publicSharing === null ? (
          sharingError === null && <p className="sa__muted">Loading…</p>
        ) : (
          <>
            {/* Saves on its own endpoint the moment it is confirmed — deliberately NOT part of
                the Save policy batch below. */}
            <label className="switch">
              <input
                type="checkbox"
                checked={publicSharing.enabled}
                disabled={sharingBusy}
                onChange={(e) => setConfirmNext(e.target.checked)}
              />
              <span className="switch__track" aria-hidden="true" />
              <span>
                <strong>Offer public access from this organization</strong>
              </span>
            </label>
            {publicSharing.enabled ? (
              <p className="sa__muted">
                On — content shared to Public Access is readable by anyone on the internet.{' '}
                {publicSharing.publicItemCount === null ? (
                  '(The number of publicly shared items is unavailable right now.)'
                ) : publicSharing.publicItemCount === 0 ? (
                  'Nothing is shared publicly yet.'
                ) : (
                  <>
                    <span className="mono-num">{publicSharing.publicItemCount}</span> item
                    {publicSharing.publicItemCount === 1 ? ' is' : 's are'} shared publicly
                    right now.
                  </>
                )}
              </p>
            ) : (
              <p className="sa__muted">
                Off (the default): public sharing, map embeds, and Open Data are all unavailable.
                Turning this on takes effect immediately — no restart, and no server access
                needed.
              </p>
            )}
          </>
        )}
      </section>

      {error !== null && <p className="sa__error">{error}</p>}

      {saved === null && error === null ? (
        <p className="sa__muted">Loading…</p>
      ) : saved === null ? null : (
        <section className="org-settings__card">
          <h2 className="org-settings__heading">Non-admin policy</h2>

          <p className="admin__note">
            Once public access is on, admins can always share content publicly. This setting
            controls whether non-admin owners (publishers who own the item) may do it too.
          </p>
          <label className="sa-check">
            <input
              type="checkbox"
              checked={nonadminPublic}
              onChange={(e) => edit(() => setNonadminPublic(e.target.checked))}
            />
            <span>Allow non-admin owners to share their layers and maps to Public Access</span>
          </label>
          {!nonadminPublic && (
            <p className="sa__muted">Off (recommended): only admins can make content public.</p>
          )}
          {publicSharing !== null && !publicSharing.enabled && nonadminPublic && (
            <p className="sa__muted">This has no effect while public access is off above.</p>
          )}

          <hr className="org-settings__rule" />

          <p className="admin__note">
            Transferring ownership hands a layer or map to another member, who becomes its owner.
            Admins can always do this (e.g. to reassign a departing employee’s content); this
            setting controls whether owners may also transfer their <strong>own</strong> items to
            a colleague without an admin.
          </p>
          <label className="sa-check">
            <input
              type="checkbox"
              checked={nonadminTransfer}
              onChange={(e) => edit(() => setNonadminTransfer(e.target.checked))}
            />
            <span>Allow members to transfer ownership of their own layers and maps</span>
          </label>
          {!nonadminTransfer && <p className="sa__muted">Off: only admins can transfer ownership.</p>}

          <div className="sign-form__actions">
            {justSaved && !dirty && <span className="org-settings__saved">Saved.</span>}
            <button
              type="button"
              className="btn btn--sm"
              onClick={() => void savePolicy()}
              disabled={busy || !dirty}
            >
              {busy ? 'Saving…' : 'Save policy'}
            </button>
          </div>
        </section>
      )}

      {confirmNext !== null && publicSharing !== null && sharingConfirm(confirmNext)}
    </div>
  )
}
