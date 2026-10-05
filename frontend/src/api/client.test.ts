import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from './client'
import { useAuthStore } from '@/lib/store'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** An unsigned JWT-shaped string; the client only reads its payload. */
function fakeToken(claims: Record<string, unknown>): string {
  const payload = btoa(JSON.stringify(claims)).replace(/=+$/, '')
  return `header.${payload}.signature`
}

describe('api client', () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  describe('a 401 on an authenticated call', () => {
    const user = { id: 'u', email: 'a@b.c' } as never
    let assign: ReturnType<typeof vi.fn>

    beforeEach(() => {
      sessionStorage.clear()
      assign = vi.fn()
      vi.stubGlobal('location', {
        ...window.location,
        assign,
        pathname: '/projects/p1',
        search: '?x=1',
      })
      vi.stubGlobal(
        'fetch',
        vi.fn().mockImplementation(() =>
          Promise.resolve(jsonResponse({ title: 'Unauthorized', status: 401 }, 401)),
        ),
      )
    })

    it('signs a password session out', async () => {
      useAuthStore.setState({ token: fakeToken({ sub: 'u' }), user })
      await expect(api.me()).rejects.toBeInstanceOf(ApiError)
      expect(useAuthStore.getState().token).toBeNull()
      expect(assign).not.toHaveBeenCalled()
    })

    it('re-authenticates an SSO session silently, once a minute', async () => {
      useAuthStore.setState({ token: fakeToken({ sub: 'u', sso: true }), user })
      await expect(api.me()).rejects.toBeInstanceOf(ApiError)
      expect(useAuthStore.getState().token).toBeNull()
      expect(assign).toHaveBeenCalledWith(
        `/api/v1/auth/oidc/login?prompt=none&next=${encodeURIComponent('/projects/p1?x=1')}`,
      )

      useAuthStore.setState({ token: fakeToken({ sub: 'u', sso: true }), user })
      await expect(api.me()).rejects.toBeInstanceOf(ApiError)
      expect(assign).toHaveBeenCalledTimes(1)
    })
  })

  it('parses an RFC 9457 problem detail into a thrown ApiError', async () => {
    const problem = {
      type: 'about:blank',
      title: 'Not Found',
      status: 404,
      detail: 'Project does not exist',
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(problem, 404))
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.getProject('missing-id')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      title: 'Not Found',
      detail: 'Project does not exist',
    })

    let caught: unknown
    try {
      await api.getProject('missing-id')
    } catch (err) {
      caught = err
    }
    expect(caught).toBeInstanceOf(ApiError)
  })

  it('attaches an Authorization: Bearer header when a token is present', async () => {
    useAuthStore.setState({ token: 'test-token', user: null })
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)

    await api.listProjects()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer test-token')
  })

  it('omits the Authorization header when no token is present', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)

    await api.listProjects()

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBeUndefined()
  })

  it('serialises cursor pagination params onto the query string', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)

    await api.listProjects({ limit: 25, cursor: 'abc123' })

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('limit=25')
    expect(url).toContain('cursor=abc123')
  })

  it('sends an Idempotency-Key header on creation requests when provided', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: '1',
        organization_id: 'org',
        name: 'Test',
        description: null,
        label_schema_id: null,
        source_connector_id: null,
        result_connector_id: null,
        source_prefix: null,
        source_glob: null,
        workflow: {},
        settings: {},
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-01-01T00:00:00Z',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await api.createProject({ name: 'Test' }, 'idem-key-1')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers['Idempotency-Key']).toBe('idem-key-1')
  })

  it('sends an empty JSON body with a scan, which the route requires', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'j1' }, 202))
    vi.stubGlobal('fetch', fetchMock)

    await api.scanProject('p1')

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/projects/p1/scan')
    expect(init.body).toBe('{}')
  })
})

/** A minimal fake standing in for the browser's XMLHttpRequest in uploadImport. */
class FakeXhr {
  static instances: FakeXhr[] = []
  method = ''
  url = ''
  status = 0
  statusText = ''
  responseText = ''
  requestHeaders: Record<string, string> = {}
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor() {
    FakeXhr.instances.push(this)
  }

  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }

  setRequestHeader(key: string, value: string): void {
    this.requestHeaders[key] = value
  }

  send(_body?: FormData): void {
    // The test drives completion manually via onload/onerror/upload.onprogress.
  }
}

describe('api.uploadImport', () => {
  beforeEach(() => {
    FakeXhr.instances = []
    useAuthStore.setState({ token: null, user: null })
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('uploads via XHR, attaches the bearer token and reports progress', async () => {
    useAuthStore.setState({ token: 'test-token', user: null })
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    const onProgress = vi.fn()

    const promise = api.uploadImport('p1', file, { format: 'coco' }, onProgress)
    const xhr = FakeXhr.instances[0]
    expect(xhr).toBeDefined()
    expect(xhr.method).toBe('POST')
    expect(xhr.url).toContain('/projects/p1/imports/upload')
    expect(xhr.requestHeaders.Authorization).toBe('Bearer test-token')

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent)
    expect(onProgress).toHaveBeenCalledWith(0.5)

    xhr.status = 202
    xhr.statusText = 'Accepted'
    xhr.responseText = JSON.stringify({ id: 'job-1', type: 'import', status: 'queued' })
    xhr.onload?.()

    await expect(promise).resolves.toMatchObject({ id: 'job-1' })
  })

  it('rejects with an ApiError parsed from the problem+json body on failure', async () => {
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    const promise = api.uploadImport('p1', file, { format: 'coco' })
    const xhr = FakeXhr.instances[0]

    xhr.status = 413
    xhr.statusText = 'Payload Too Large'
    xhr.responseText = JSON.stringify({
      type: 'about:blank',
      title: 'Payload Too Large',
      status: 413,
      detail: 'File exceeds 64 MiB',
    })
    xhr.onload?.()

    await expect(promise).rejects.toMatchObject({
      name: 'ApiError',
      status: 413,
      detail: 'File exceeds 64 MiB',
    })
  })

  it('omits attribute_mapping from the form when empty', async () => {
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    const sendSpy = vi.spyOn(FakeXhr.prototype, 'send')
    void api.uploadImport('p1', file, { format: 'coco', attribute_mapping: {} })

    const [form] = sendSpy.mock.calls[0] as [FormData]
    expect(form.has('attribute_mapping')).toBe(false)
  })
})
