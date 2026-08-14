"""Image decoders: PNG, GIF and the Netpbm family, decoded to raw RGBA.

These three are the formats we decode ourselves, which is what lets the
browser show an image without asking anything outside the project. JPEG is not
here -- it stays on the optional Pillow path, and docs/dependencies.md scopes
what writing our own would take.

Thin shim: the decoders live in Rust, in the `feetbrowser_engine` extension.
They are the part of the renderer that parses bytes a stranger sent us, and
moving them out of Python bought both the speed (a photo decodes in about a
fortieth of the time) and a place where every read is bounds-checked on
purpose rather than by the interpreter.

Every decoder returns ``(width, height, rgba)`` where rgba is 4 bytes per
pixel, which is what raster.Surface.blit_rgba consumes.
"""

from feetbrowser_engine import (MAX_INFLATED, MAX_PIXELS, ImageError, decode,
                                decode_gif, decode_png, decode_pnm, resize,
                                sniff)

__all__ = ["ImageError", "MAX_PIXELS", "MAX_INFLATED", "decode", "decode_png",
           "decode_gif", "decode_pnm", "sniff", "resize"]
