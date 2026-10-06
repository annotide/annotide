import { useEffect, useState } from 'react'
import QRCode from 'qrcode'

/** A QR code for `value`, drawn in the browser; nothing leaves the page. */
export function QrCode({ value, label }: { value: string; label: string }): JSX.Element | null {
  const [src, setSrc] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    QRCode.toDataURL(value, { margin: 1, width: 192, errorCorrectionLevel: 'M' })
      .then((url) => current && setSrc(url))
      .catch(() => current && setSrc(null))
    return () => {
      current = false
    }
  }, [value])

  if (!src) return null
  // White background regardless of theme: scanners need the contrast.
  return <img src={src} alt={label} width={192} height={192} className="rounded bg-white p-1" />
}
