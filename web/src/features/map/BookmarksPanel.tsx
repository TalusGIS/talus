import { useState } from 'react'
import { ArrowDown, ArrowUp, Bookmark, Camera, Check, Pencil, Trash2 } from 'lucide-react'

import type { WebMapBookmark } from '@/lib/api/client'
import CloseButton from '@/components/CloseButton'

// Map bookmarks — saved named viewpoints (decision-bookmarks.md). A left-dock control opens this
// panel; clicking a bookmark flies the map to its extent (center+zoom). Editing (add-current-view,
// rename, reorder, update-extent, delete) is gated on the map's edit permission; a viewer sees the
// list read-only. Extent-only: a bookmark never snapshots layer visibility/symbology. Bookmarks
// persist only when the MAP is saved — hence the "save to keep" hint while there are unsaved edits.

export interface BookmarksPanelProps {
  bookmarks: WebMapBookmark[]
  /** Can the caller manage bookmarks (add/rename/reorder/update/delete)? Mirrors the Save-button gate. */
  canEdit: boolean
  /** Are there unsaved bookmark edits? Drives the "save the map to keep your bookmarks" hint. */
  dirty: boolean
  onNavigate: (bm: WebMapBookmark) => void
  onAdd: (name: string) => void
  onRename: (index: number, name: string) => void
  /** Overwrite a bookmark's extent with the current map view (AGO "Set bookmark extent"). */
  onUpdateExtent: (index: number) => void
  onReorder: (index: number, dir: -1 | 1) => void
  onDelete: (index: number) => void
  onClose: () => void
}

export default function BookmarksPanel(props: BookmarksPanelProps) {
  const { bookmarks, canEdit, dirty } = props
  const [newName, setNewName] = useState('')
  const [editIdx, setEditIdx] = useState<number | null>(null)
  const [editName, setEditName] = useState('')

  const submitAdd = (): void => {
    const n = newName.trim()
    if (!n) return
    props.onAdd(n)
    setNewName('')
  }

  const beginEdit = (i: number, name: string): void => {
    setEditIdx(i)
    setEditName(name)
  }
  const commitEdit = (): void => {
    if (editIdx === null) return
    const n = editName.trim()
    if (n) props.onRename(editIdx, n)
    setEditIdx(null)
    setEditName('')
  }

  return (
    <div className="map__panel bookmarks">
      <div className="add-layers__header">
        <span className="add-layers__title">Bookmarks</span>
        <CloseButton onClick={props.onClose} className="add-layers__close" />
      </div>

      {canEdit && (
        <div className="bookmarks__add">
          <input
            className="add-layers__search bookmarks__addinput"
            placeholder="Name this view…"
            value={newName}
            maxLength={80}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitAdd()
            }}
          />
          <button
            type="button"
            className="btn btn--sm"
            disabled={!newName.trim()}
            title="Add the current map view as a bookmark"
            onClick={submitAdd}
          >
            + Bookmark
          </button>
        </div>
      )}

      {bookmarks.length === 0 ? (
        <p className="add-layers__empty">
          No bookmarks yet.{canEdit ? ' Name the current view above to add one.' : ''}
        </p>
      ) : (
        <ul className="add-layers__list bookmarks__list">
          {bookmarks.map((bm, i) => (
            <li key={i} className="bookmarks__row">
              {editIdx === i ? (
                <>
                  <input
                    className="add-layers__search bookmarks__renameinput"
                    value={editName}
                    maxLength={80}
                    autoFocus
                    onChange={(e) => setEditName(e.target.value)}
                    onBlur={commitEdit}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitEdit()
                      else if (e.key === 'Escape') {
                        setEditIdx(null)
                        setEditName('')
                      }
                    }}
                  />
                  {/* Edit-mode-only actions. preventDefault on mousedown keeps the rename input
                      focused so clicking these doesn't blur-commit and drop out of edit mode. */}
                  <span className="bookmarks__actions">
                    <button
                      type="button"
                      className="bookmarks__act"
                      title="Update this bookmark to the current map view"
                      aria-label={`Update “${bm.name}” to current view`}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => props.onUpdateExtent(i)}
                    >
                      <Camera size={13} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="bookmarks__act"
                      title="Done"
                      aria-label={`Done editing “${bm.name}”`}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={commitEdit}
                    >
                      <Check size={13} aria-hidden="true" />
                    </button>
                  </span>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="bookmarks__go"
                    title={`Fly to “${bm.name}”`}
                    onClick={() => props.onNavigate(bm)}
                  >
                    <Bookmark size={13} aria-hidden="true" />
                    <span className="bookmarks__name">{bm.name}</span>
                  </button>
                  {canEdit && (
                    <span className="bookmarks__actions">
                      <button
                        type="button"
                        className="bookmarks__act"
                        title="Edit (rename or update view)"
                        aria-label={`Edit “${bm.name}”`}
                        onClick={() => beginEdit(i, bm.name)}
                      >
                        <Pencil size={13} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="bookmarks__act"
                        title="Move up"
                        aria-label={`Move “${bm.name}” up`}
                        disabled={i === 0}
                        onClick={() => props.onReorder(i, -1)}
                      >
                        <ArrowUp size={13} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="bookmarks__act"
                        title="Move down"
                        aria-label={`Move “${bm.name}” down`}
                        disabled={i === bookmarks.length - 1}
                        onClick={() => props.onReorder(i, 1)}
                      >
                        <ArrowDown size={13} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="bookmarks__act bookmarks__act--danger"
                        title="Delete"
                        aria-label={`Delete “${bm.name}”`}
                        onClick={() => props.onDelete(i)}
                      >
                        <Trash2 size={13} aria-hidden="true" />
                      </button>
                    </span>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {canEdit && dirty && (
        <p className="bookmarks__hint">Save the map to keep your bookmarks.</p>
      )}
    </div>
  )
}




