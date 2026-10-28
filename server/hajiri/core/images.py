"""
Server-side image handling with Pillow.

`square_jpeg` does on the server what the browser does before a profile photo upload (AvatarEditor.tsx squareJpeg,
`Hajiri.squareJpeg` in static/js/app.js): centre-crop to a square, scale to 320 x 320 and encode as JPEG at quality 85.
A photo posted without the page's script (JavaScript off, an API client) therefore ends up the same size and format.
EXIF orientation is applied first and metadata (location, camera) is dropped.

    from hajiri.core.images import square_jpeg
    data = square_jpeg(uploaded_bytes)              # bytes of a 320 px JPEG, or BadRequestError
"""
from __future__ import annotations

import io

from .errors import BadRequestError

AVATAR_SIZE = 320
JPEG_QUALITY = 85                 # canvas.toBlob(..., 'image/jpeg', 0.85)
MAX_PIXELS = 40_000_000           # refuse decompression bombs well above any phone photo


def square_jpeg(data: bytes, size: int = AVATAR_SIZE, quality: int = JPEG_QUALITY) -> bytes:
    """A centre-cropped square JPEG of `size` pixels from any image Pillow can read (JPEG, PNG, WebP, ...)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise BadRequestError('The photo is too large.')
            img = ImageOps.exif_transpose(img)
            if img.mode not in ('RGB', 'L'):
                # Transparent PNG/WebP: a JPEG has no alpha; the browser's canvas export turns it black, so do we.
                background = Image.new('RGB', img.size, (0, 0, 0))
                rgba = img.convert('RGBA')
                background.paste(rgba, mask=rgba.getchannel('A'))
                img = background
            out = ImageOps.fit(img.convert('RGB'), (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            buf = io.BytesIO()
            out.save(buf, format='JPEG', quality=quality, optimize=True)
            return buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise BadRequestError('That file is not a photo we can read.') from None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of an image, or None if Pillow cannot read it."""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except (UnidentifiedImageError, OSError):
        return None
