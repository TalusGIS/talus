import type { ShareGroup, ShareLevel } from '@/lib/api'

/**
 * The catalog badge level for a share-state response — broadest tier wins (public ⊃ organization ⊃
 * groups ⊃ owner), mirroring the server's `_layer_share_level` / `_owned_summaries`. Used by both
 * Manage dialogs to patch the row badge live after a save (no reload).
 */
// eslint-disable-next-line react-refresh/only-export-components -- deliberate co-location (both Manage dialogs import it); HMR full-reload accepted
export function shareBadgeLevel(shares: {
  publicAccess: boolean
  organizationOnly: boolean
  groups: unknown[]
}): ShareLevel {
  if (shares.publicAccess) return 'public'
  if (shares.organizationOnly) return 'organization'
  if (shares.groups.length > 0) return 'groups'
  return 'owner'
}

/**
 * Talus 25 — the shared Sharing controls for a catalog item's Manage dialog: the mutually-exclusive
 * level radios (Owner Only · Organization · Public) plus the additive Groups checkboxes. Extracted
 * from ManageMapDialog so web maps AND layers render an IDENTICAL Sharing section (the slice's whole
 * thesis is parity) — there's one implementation, so the two can't drift.
 *
 * It is presentational: the parent owns the state (current level, selected groups, the policy flags)
 * and the persistence. The Public tier is always SHOWN and is interactive only when this caller may
 * use it: `publicEnabled` (the org's F6 master switch, admin-togglable in Admin → Settings) and
 * `mayTogglePublic` (= admin, or a non-admin when org policy allows). The server re-enforces all of
 * it — this is a faithful mirror, not the gate.
 *
 * ★ Why the row is rendered even when the master switch is off (clean-box run, 2026-08-25):
 * it used to be hidden outright in that case, which left an admin hunting for a tier that simply
 * wasn't on screen — while the NON-admin case, one line below, has always explained itself
 * ("Only administrators can enable this…"). That asymmetry was backwards: the switch case was the
 * HARDER one to resolve and it was the one saying nothing at all. Both unavailable states now name
 * what they need — and as of decision-public-sharing-reachable.md what the switch case needs is a
 * setting in Admin → Settings, not shell access, an .env edit and a container recreate.
 */
export default function SharingSection({
  itemNoun,
  groups,
  level,
  onLevelChange,
  selected,
  onToggleGroup,
  publicEnabled,
  mayTogglePublic,
  hidePublic = false,
}: {
  itemNoun: 'map' | 'layer' | 'raster' | 'table' | 'reference' | 'field project' | 'story' | 'dashboard' | 'report' // 'table' = the wizard's non-spatial-table sharing step; 'reference' = an external catalog item; 'field project' = an uploaded .qgz artifact; 'story'/'dashboard' = Talus+ items (paid/web reuses this section)
  groups: ShareGroup[]
  level: 'owner' | 'org' | 'public'
  onLevelChange: (level: 'owner' | 'org' | 'public') => void
  selected: Set<string>
  onToggleGroup: (groupId: string) => void
  publicEnabled: boolean
  mayTogglePublic: boolean
  /** Omit the Public tier entirely — for item kinds that are NEVER public by design (Talus+
   *  reports, decision-scheduled-reports.md F6), where a disabled radio with an "ask your
   *  administrator" sentence would be the wrong story. */
  hidePublic?: boolean
}) {
  // Usable only when the deployment allows public access AND this caller may set it.
  const publicAvailable = publicEnabled && mayTogglePublic
  return (
    <>
      <p className="modal__section-title">Sharing level</p>
      <label className="share-dialog__row">
        <input
          type="radio"
          name="share-level"
          checked={level === 'owner'}
          onChange={() => onLevelChange('owner')}
        />
        <span className="share-dialog__text">
          <span className="share-dialog__name">Owner Only</span>
          <span className="share-dialog__hint">Only you can view this {itemNoun}.</span>
        </span>
      </label>

      <label className="share-dialog__row">
        <input
          type="radio"
          name="share-level"
          checked={level === 'org'}
          onChange={() => onLevelChange('org')}
        />
        <span className="share-dialog__text">
          <span className="share-dialog__name">Organization Only</span>
          <span className="share-dialog__hint">
            Everyone signed in to your organization can view this {itemNoun}.
          </span>
        </span>
      </label>

      {/* Two independent reasons Public can be unavailable, and they need different sentences:
          the org's master switch is off (nobody can use the tier, admins included), or this caller
          lacks permission (an admin can grant it in the org settings). Neither is silent, and
          since decision-public-sharing-reachable.md neither sentence sends the reader to a shell:
          this copy used to name an environment variable and a container restart, which is the
          exact dead end that left three shipped features unreachable. */}
      {!hidePublic && <label
        className={`share-dialog__row${publicAvailable ? '' : ' share-dialog__row--disabled'}`}
      >
        <input
          type="radio"
          name="share-level"
          checked={level === 'public'}
          disabled={!publicAvailable}
          onChange={() => onLevelChange('public')}
        />
        <span className="share-dialog__text">
          <span className="share-dialog__name">Public Access</span>
          <span className="share-dialog__hint">
            Anyone on the public internet can view this {itemNoun} — no sign-in required.
            {!publicEnabled
              ? ' Public sharing is turned off for this organization — an administrator can turn' +
                ' it on in Admin → Sharing & access. It is off by default so a new install is never' +
                ' internet-facing by accident.'
              : !mayTogglePublic
                ? ' Only administrators can enable this in your organization.'
                : ''}
          </span>
        </span>
      </label>}
      {publicEnabled && level === 'public' && (
        <p className="share-dialog__public-warning" role="alert">
          ⚠ This {itemNoun} will be visible to <strong>anyone on the internet</strong>. Make sure
          its data is cleared for public release. You can turn this off later, but copies made while
          it’s public can’t be recalled.
        </p>
      )}

      <p className="modal__section-title share-dialog__groups-title">Groups</p>
      {groups.length === 0 ? (
        <p className="share-dialog__hint">You aren’t a member of any groups to share with.</p>
      ) : (
        groups.map((g) => (
          <label key={g.group_id} className="share-dialog__row">
            <input
              type="checkbox"
              checked={selected.has(g.group_id)}
              onChange={() => onToggleGroup(g.group_id)}
            />
            <span className="share-dialog__text">
              <span className="share-dialog__name">{g.display_name}</span>
            </span>
          </label>
        ))
      )}
    </>
  )
}
