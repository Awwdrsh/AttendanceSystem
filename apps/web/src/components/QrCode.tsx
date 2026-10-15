import qrcode from 'qrcode-generator';
import { useMemo } from 'react';

/** A QR code drawn as SVG paths (no canvas, no network); dark modules on a white quiet zone so phones read it in dark mode too. */
export function QrCode({ value, size = 200, label }: { value: string; size?: number; label?: string }) {
  const { n, d } = useMemo(() => {
    const qr = qrcode(0, 'M');
    qr.addData(value);
    qr.make();
    const count = qr.getModuleCount();
    let path = '';
    for (let r = 0; r < count; r++) for (let c = 0; c < count; c++) if (qr.isDark(r, c)) path += `M${c + 4} ${r + 4}h1v1h-1z`;
    return { n: count + 8, d: path };
  }, [value]);
  return (
    <svg width={size} height={size} viewBox={`0 0 ${n} ${n}`} role="img" aria-label={label ?? 'QR code'} shapeRendering="crispEdges" style={{ background: '#fff', borderRadius: 8 }}>
      <path d={d} fill="#000" />
    </svg>
  );
}
