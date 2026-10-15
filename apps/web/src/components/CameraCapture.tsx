import { Alert, Box, Button, Group, Loader, Modal, Stack, Text } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import { IconCamera, IconCameraOff, IconRefresh, IconUpload } from '@tabler/icons-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';

const MAX_W = 480;
const MAX_BYTES = 280 * 1024;
const ICON = { size: 16, stroke: 1.6 } as const;

/** Draw a video frame or image onto a canvas at most 480 px wide and encode it as a JPEG under the upload limit. */
function toJpeg(src: CanvasImageSource, w: number, h: number, mirror: boolean): string {
  const scale = Math.min(1, MAX_W / w);
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext('2d')!;
  if (mirror) { ctx.translate(canvas.width, 0); ctx.scale(-1, 1); }
  ctx.drawImage(src, 0, 0, canvas.width, canvas.height);
  let q = 0.8; let out = canvas.toDataURL('image/jpeg', q);
  while (out.length * 0.75 > MAX_BYTES && q > 0.35) { q -= 0.1; out = canvas.toDataURL('image/jpeg', q); }
  return out;
}

type Phase = 'starting' | 'live' | 'captured' | 'unavailable';

/**
 * Selfie for a clock-in from home. Uses the front camera when the browser allows it; otherwise (no camera, permission
 * refused, or the page is not on HTTPS) falls back to the phone's own camera through a file picker.
 */
export function CameraCapture({ opened, onClose, onConfirm, title, confirmLabel, busy, footer }: {
  opened: boolean; onClose: () => void; onConfirm: (photo: string) => void; title: string; confirmLabel: string; busy?: boolean; footer?: ReactNode;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>('starting');
  const [photo, setPhoto] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const phone = useMediaQuery('(max-width: 36em)');

  const stop = () => { stream.current?.getTracks().forEach((t) => t.stop()); stream.current = null; };
  const start = async () => {
    setPhoto(null); setProblem(null); setPhase('starting');
    if (!navigator.mediaDevices?.getUserMedia) { setProblem(window.isSecureContext ? 'This browser cannot open the camera.' : 'The camera only works over a secure (HTTPS) connection.'); setPhase('unavailable'); return; }
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
      stream.current = s;
      if (video.current) { video.current.srcObject = s; await video.current.play().catch(() => undefined); }
      setPhase('live');
    } catch (e) {
      const name = (e as { name?: string }).name;
      setProblem(name === 'NotAllowedError' ? 'Camera permission was refused. Allow the camera for this site, or take the photo with your phone camera instead.' : 'No camera was found on this device.');
      setPhase('unavailable');
    }
  };
  useEffect(() => { if (opened) void start(); else { stop(); setPhoto(null); } return stop; }, [opened]); // eslint-disable-line react-hooks/exhaustive-deps

  const snap = () => {
    const v = video.current;
    if (!v || !v.videoWidth) return;
    setPhoto(toJpeg(v, v.videoWidth, v.videoHeight, true));
    stop(); setPhase('captured');
  };
  const fromFile = (f: File | undefined) => {
    if (!f) return;
    const url = URL.createObjectURL(f);
    const img = new Image();
    img.onload = () => { setPhoto(toJpeg(img, img.naturalWidth, img.naturalHeight, false)); URL.revokeObjectURL(url); setPhase('captured'); };
    img.onerror = () => { setProblem('That file is not a photo we can read.'); URL.revokeObjectURL(url); };
    img.src = url;
  };

  return (
    <Modal opened={opened} onClose={() => { stop(); onClose(); }} title={title} centered fullScreen={!!phone} size="md" radius="lg">
      <Stack gap="md">
        <Box pos="relative" style={{ aspectRatio: '4 / 3', borderRadius: 14, overflow: 'hidden', background: 'var(--hj-ink, #161c26)' }}>
          <video ref={video} playsInline muted aria-label="Camera preview"
            style={{ width: '100%', height: '100%', objectFit: 'cover', transform: 'scaleX(-1)', display: phase === 'live' || phase === 'starting' ? 'block' : 'none' }} />
          {photo ? <img src={photo} alt="Your photo" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} /> : null}
          {phase === 'live' ? (
            <Box aria-hidden pos="absolute" style={{ inset: 0, display: 'grid', placeItems: 'center', pointerEvents: 'none' }}>
              <Box style={{ width: '46%', aspectRatio: '3 / 4', borderRadius: '50%', boxShadow: '0 0 0 9999px rgba(10, 14, 20, 0.35)', border: '2px solid rgba(255,255,255,0.85)' }} />
            </Box>
          ) : null}
          {phase === 'starting' ? <Box pos="absolute" style={{ inset: 0, display: 'grid', placeItems: 'center' }}><Loader color="gray.0" size="sm" /></Box> : null}
          {phase === 'unavailable' && !photo ? (
            <Stack pos="absolute" style={{ inset: 0 }} align="center" justify="center" gap={6} p="md">
              <IconCameraOff size={28} stroke={1.5} color="rgba(255,255,255,0.8)" />
              <Text size="sm" c="gray.3" ta="center">Camera not available</Text>
            </Stack>
          ) : null}
        </Box>

        {problem ? <Alert variant="light" color="yellow">{problem}</Alert> : null}
        <Text size="xs" c="dimmed">{phase === 'captured' ? 'Happy with it? Your photo is encrypted, seen only by you, your manager and HR, and deleted automatically after the retention period.' : 'Center your face in the oval. Only this one photo is taken; the camera turns off right after.'}</Text>

        <input ref={file} type="file" accept="image/*" capture="user" hidden onChange={(e) => { fromFile(e.currentTarget.files?.[0]); e.currentTarget.value = ''; }} />
        <Group justify="space-between" wrap="wrap" gap="xs">
          {phase === 'captured'
            ? <Button variant="default" leftSection={<IconRefresh {...ICON} />} onClick={() => void start()} disabled={!!busy}>Retake</Button>
            : <Button variant="subtle" color="gray" leftSection={<IconUpload {...ICON} />} onClick={() => file.current?.click()}>Use phone camera</Button>}
          {phase === 'captured' && photo
            ? <Button leftSection={<IconCamera {...ICON} />} loading={!!busy} onClick={() => onConfirm(photo)}>{confirmLabel}</Button>
            : <Button leftSection={<IconCamera {...ICON} />} disabled={phase !== 'live'} onClick={snap}>Take photo</Button>}
        </Group>
        {footer}
      </Stack>
    </Modal>
  );
}
