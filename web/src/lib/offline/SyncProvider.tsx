/**
 * SyncProvider (Talus 35) — the app-wide online/offline context for the web editor.
 *
 * SCOPE (re-scoped 2026-06-22, decision-disconnected-editing.md §11): editing is **online-only**, like
 * ESRI's web editor. Offline, the PWA still loads and lets you VIEW already-cached areas, but a write
 * is blocked with a "reconnect to edit" message rather than queued. The offline edit-queue (outbox +
 * drain + conflict reconcile + token-renewal-for-replay) was removed — its failure surface (token
 * expiry offline, navigator.onLine unreliability, per-panel offline reads) was unbounded for a use
 * case ESRI itself doesn't offer; real field offline-editing is owned by the QField + QFieldCloud track.
 *
 * This provider therefore owns just two things: the online signal, and a single online-only write path.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useAuth } from 'react-oidc-context'

import { ApiError, ConflictError, resolveAssetApi } from '@/lib/api'

export type WriteKind = 'create' | 'update' | 'move' | 'delete'

/** A failure that means "the network is unreachable" (fetch rejects with a TypeError, or we're
 *  offline) rather than "the server said no" — treated as offline, not a hard error. */
function isOfflineError(err: unknown): boolean {
  if (typeof navigator !== 'undefined' && !navigator.onLine) return true
  return err instanceof TypeError
}

export interface SubmitWriteInput {
  kind: WriteKind
  layerId: string
  featureId?: string | null
  /** create body / edit attrs / `{lon,lat}` for a move; omitted for a delete. */
  payload?: unknown
  /** the T34 version token for update/move/delete. */
  baseVersion?: string | null
}

/** The outcome of a write. `ok` = applied; `offline` = blocked (we're offline — reconnect to edit);
 *  `conflict` = a 409 (caller shows the inline T34 panel); `error` = a real failure to surface. */
export type WriteResult =
  | { status: 'ok'; result?: unknown }
  | { status: 'offline' }
  | { status: 'conflict'; current: Record<string, unknown> | null }
  | { status: 'error'; error: ApiError }

interface SyncContextValue {
  online: boolean
  submitWrite: (input: SubmitWriteInput) => Promise<WriteResult>
}

const SyncContext = createContext<SyncContextValue | null>(null)

// eslint-disable-next-line react-refresh/only-export-components -- the standard provider+hook co-location; HMR full-reload accepted
export function useSync(): SyncContextValue {
  const ctx = useContext(SyncContext)
  if (!ctx) throw new Error('useSync must be used within <SyncProvider>')
  return ctx
}

function isOnline(): boolean {
  return typeof navigator === 'undefined' ? true : navigator.onLine
}

export function SyncProvider({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const [online, setOnline] = useState<boolean>(isOnline())

  useEffect(() => {
    const goOnline = () => setOnline(true)
    const goOffline = () => setOnline(false)
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
    }
  }, [])

  const submitWrite = useCallback(
    async (input: SubmitWriteInput): Promise<WriteResult> => {
      const api = resolveAssetApi(input.layerId)
      if (!api) return { status: 'error', error: new ApiError(0, `unknown layer ${input.layerId}`) }

      // Editing is online-only: offline (or a write that fails on the network) is blocked, not queued.
      if (!isOnline()) return { status: 'offline' }
      const u = auth.user
      const token = u && !u.expired && u.access_token ? u.access_token : null
      if (!token) return { status: 'error', error: new ApiError(401, 'not signed in') }

      try {
        if (input.kind === 'create') {
          const result = await api.create(input.payload, token)
          return { status: 'ok', result }
        }
        if (input.kind === 'delete') {
          await api.remove(input.featureId as string, token, input.baseVersion ?? '')
          return { status: 'ok' }
        }
        const result = await api.update(
          input.featureId as string,
          input.payload,
          token,
          input.baseVersion ?? '',
        )
        return { status: 'ok', result }
      } catch (err) {
        if (isOfflineError(err)) return { status: 'offline' } // network dropped mid-write
        if (err instanceof ConflictError) {
          return { status: 'conflict', current: (err.current as Record<string, unknown> | null) ?? null }
        }
        if (err instanceof ApiError) return { status: 'error', error: err }
        return { status: 'error', error: new ApiError(0, 'write failed') }
      }
    },
    [auth],
  )

  const value = useMemo<SyncContextValue>(() => ({ online, submitWrite }), [online, submitWrite])

  return <SyncContext.Provider value={value}>{children}</SyncContext.Provider>
}
