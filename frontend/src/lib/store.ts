/**
 * Zustand stores: authentication state (persisted) and UI preferences.
 */
import { create } from 'zustand'
import type { Task, User } from '@/api/types'

const AUTH_STORAGE_KEY = 'annotation.auth'
const UI_STORAGE_KEY = 'annotation.ui'

interface PersistedAuth {
  token: string | null
  user: User | null
}

function readPersistedAuth(): PersistedAuth {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY)
    if (!raw) return { token: null, user: null }
    const parsed = JSON.parse(raw) as Partial<PersistedAuth>
    return { token: parsed.token ?? null, user: parsed.user ?? null }
  } catch {
    return { token: null, user: null }
  }
}

function writePersistedAuth(state: PersistedAuth): void {
  try {
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Ignore storage failures (private browsing, quota, disabled storage, …).
  }
}

function clearPersistedAuth(): void {
  try {
    localStorage.removeItem(AUTH_STORAGE_KEY)
  } catch {
    // Ignore storage failures.
  }
}

interface AuthState {
  token: string | null
  user: User | null
  login: (token: string, user: User) => void
  logout: () => void
}

const initialAuth = readPersistedAuth()

export const useAuthStore = create<AuthState>((set) => ({
  token: initialAuth.token,
  user: initialAuth.user,
  login: (token, user) => {
    writePersistedAuth({ token, user })
    set({ token, user })
  },
  logout: () => {
    clearPersistedAuth()
    set({ token: null, user: null })
  },
}))

export type Theme = 'light' | 'dark'

export type Tool = 'select' | 'bbox' | 'rbox' | 'polygon' | 'polyline' | 'point' | 'mask'

interface PersistedUi {
  theme: Theme
  sidebarOpen: boolean
}

function readPersistedUi(): PersistedUi {
  try {
    const raw = localStorage.getItem(UI_STORAGE_KEY)
    if (!raw) return { theme: 'dark', sidebarOpen: true }
    const parsed = JSON.parse(raw) as Partial<PersistedUi>
    return {
      theme: parsed.theme === 'light' ? 'light' : 'dark',
      sidebarOpen: parsed.sidebarOpen ?? true,
    }
  } catch {
    return { theme: 'dark', sidebarOpen: true }
  }
}

function writePersistedUi(state: PersistedUi): void {
  try {
    localStorage.setItem(UI_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Ignore storage failures.
  }
}

interface UiState {
  theme: Theme
  sidebarOpen: boolean
  currentTool: Tool
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
  setSidebarOpen: (open: boolean) => void
  toggleSidebar: () => void
  setCurrentTool: (tool: Tool) => void
}

const initialUi = readPersistedUi()

export const useUiStore = create<UiState>((set, get) => ({
  theme: initialUi.theme,
  sidebarOpen: initialUi.sidebarOpen,
  currentTool: 'select',
  setTheme: (theme) => {
    writePersistedUi({ theme, sidebarOpen: get().sidebarOpen })
    set({ theme })
  },
  toggleTheme: () => {
    const next: Theme = get().theme === 'dark' ? 'light' : 'dark'
    writePersistedUi({ theme: next, sidebarOpen: get().sidebarOpen })
    set({ theme: next })
  },
  setSidebarOpen: (open) => {
    writePersistedUi({ theme: get().theme, sidebarOpen: open })
    set({ sidebarOpen: open })
  },
  toggleSidebar: () => {
    const next = !get().sidebarOpen
    writePersistedUi({ theme: get().theme, sidebarOpen: next })
    set({ sidebarOpen: next })
  },
  setCurrentTool: (tool) => set({ currentTool: tool }),
}))

/**
 * The task this browser currently holds a lock on (WF-3) — an annotate or a
 * review task, never both — kept across the `/annotate` → `/annotate/:itemId`
 * (or `/review/…`) navigation. In-memory only: a held lock is tied to this
 * browser session and should not survive a reload (the server-side lock
 * expires on its own via `locked_until`).
 */
interface TaskState {
  task: Task | null
  setTask: (task: Task | null) => void
  clearTask: () => void
}

export const useTaskStore = create<TaskState>((set) => ({
  task: null,
  setTask: (task) => set({ task }),
  clearTask: () => set({ task: null }),
}))

/**
 * The item each project's annotator last saved or submitted in this browser
 * session: the source for "copy from previous item" (TOOL-7). In memory only.
 */
interface RecentItemState {
  lastSavedItem: Record<string, string>
  rememberSavedItem: (projectId: string, itemId: string) => void
}

export const useRecentItemStore = create<RecentItemState>((set) => ({
  lastSavedItem: {},
  rememberSavedItem: (projectId, itemId) =>
    set((state) => ({ lastSavedItem: { ...state.lastSavedItem, [projectId]: itemId } })),
}))
