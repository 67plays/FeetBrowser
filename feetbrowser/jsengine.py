"""The JavaScript engine, re-exported under one set of names.

The browser talks to `Interpreter`, `JSException` and `UNDEFINED` and does not
care what is behind them. There is one engine: the `feetbrowser_engine`
extension module, whose interpreter, DOM bridge and renderer inner loops are
compiled to Rust (see rust/).
"""

from feetbrowser_engine import Interpreter, JSException, UNDEFINED

__all__ = ["Interpreter", "JSException", "UNDEFINED"]
