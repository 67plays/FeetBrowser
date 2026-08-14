"""JS engine (Rust extension, `feetbrowser_engine`).

Thin shim: the engine itself is implemented in Rust and built as the
`feetbrowser_engine` extension module.
"""

from feetbrowser_engine import Interpreter, JSException, UNDEFINED

__all__ = ["Interpreter", "JSException", "UNDEFINED"]
