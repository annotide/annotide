/**
 * `url` if it is safe to put in an `href`, else `undefined`.
 *
 * Run and training links come from model metadata and ML platforms, not from
 * this server, so a `javascript:` or `data:` URL must not become a link. Only
 * http(s) is allowed; a relative URL resolves against this page's origin.
 */
export function safeHref(url: unknown): string | undefined {
  if (typeof url !== 'string' || url === '') return undefined
  try {
    const { protocol } = new URL(url, window.location.href)
    return protocol === 'http:' || protocol === 'https:' ? url : undefined
  } catch {
    return undefined
  }
}
