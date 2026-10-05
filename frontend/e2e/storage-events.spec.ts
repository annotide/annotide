/**
 * Event-driven discovery end to end (SRC-3): a blob lands in Azurite, an
 * Event Grid notification for it reaches the storage-event endpoint with
 * nothing but the connector's token, and the compose `worker` registers
 * that one blob as an item without listing the container.
 *
 * Turns events on for the demo connector for the length of the test and off
 * again, so it skips when an administrator already turned them on there (it
 * would replace their token). Works on a scratch project with a source
 * prefix of its own, hard-deleted at the end; the blob stays in Azurite
 * under `e2e-events/`.
 */
import { createScratchProject, expect, test, waitForJob } from './support'

/** A valid 1×1 PNG. */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

interface ConnectorRow {
  id: string
  events_enabled: boolean
  config: { container?: string }
}

test('an Event Grid notification registers the blob it names as an item', async ({
  api,
  playwright,
  baseURL,
}) => {
  const prefix = `e2e-events/${Date.now()}/`
  const project = await createScratchProject(api, 'E2E events', '*.png', prefix)
  const store = await playwright.request.newContext({ baseURL })
  let connectorId: string | null = null
  try {
    expect(project.itemIds, 'a fresh prefix starts empty').toHaveLength(0)
    const { source_connector_id: sourceId } = (await (
      await api.get(`/api/v1/projects/${project.id}`)
    ).json()) as { source_connector_id: string }
    const connector = (await (
      await api.get(`/api/v1/connectors/${sourceId}`)
    ).json()) as ConnectorRow
    test.skip(connector.events_enabled, 'the demo connector already has storage events on')

    // The blob arrives: a write-scoped signed URL, as a browser upload would use.
    const minted = await api.post(`/api/v1/projects/${project.id}/uploads`, {
      data: { files: [{ path: 'arrived.png', content_type: 'image/png' }] },
    })
    expect(minted.status(), await minted.text()).toBe(200)
    const [slot] = ((await minted.json()) as {
      uploads: Array<{ url: string; method: string; headers: Record<string, string> }>
    }).uploads
    const put = await store.fetch(slot.url, { method: slot.method, headers: slot.headers, data: PNG })
    expect(put.ok(), `PUT answered ${put.status()}`).toBeTruthy()

    const token = await api.post(`/api/v1/connectors/${connector.id}/events/token`)
    expect(token.status(), await token.text()).toBe(201)
    connectorId = connector.id
    const { token: secret, path } = (await token.json()) as { token: string; path: string }

    // What Event Grid posts for a new block blob, trimmed.
    const delivered = await store.post(`${path}?token=${encodeURIComponent(secret)}`, {
      data: [
        {
          id: crypto.randomUUID(),
          eventType: 'Microsoft.Storage.BlobCreated',
          subject: `/blobServices/default/containers/${connector.config.container}/blobs/${prefix}arrived.png`,
          eventTime: new Date().toISOString(),
          data: { api: 'PutBlob', contentType: 'image/png', contentLength: PNG.length },
          dataVersion: '',
        },
      ],
    })
    expect(delivered.status(), await delivered.text()).toBe(202)
    const { job_ids: jobIds } = (await delivered.json()) as { job_ids: string[] }
    expect(jobIds.length).toBeGreaterThan(0)
    for (const jobId of jobIds) await waitForJob(api, jobId)

    const items = (await (
      await api.get(`/api/v1/projects/${project.id}/items`, { params: { limit: 10 } })
    ).json()) as { items: Array<{ path: string; width: number | null }> }
    expect(items.items).toEqual([expect.objectContaining({ path: `${prefix}arrived.png`, width: 1 })])
  } finally {
    if (connectorId) await api.delete(`/api/v1/connectors/${connectorId}/events/token`)
    await store.dispose()
    await api.delete(`/api/v1/projects/${project.id}`)
  }
})
