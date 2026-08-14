"""Windows, events and the main loop, without a GUI toolkit.

Tk gave the browser three things beyond drawing: a place to put pixels, a
stream of input events with Tk's naming (``<Button-1>``, ``<Control-l>``,
``<MouseWheel>``), and ``after()`` for deferred work. This module supplies
all three.

``Window`` is the headless base: it owns the binding table, the timer queue
and the loop that drains them. That is the whole event model, and it runs
with no display at all -- which is what lets a page be rendered, driven and
asserted on in a test with nothing installed. Platform windows subclass it
and add a surface to present and a source of real input; everything above
them only ever sees the Tk-shaped API.
"""
import heapq
import time

from . import canvas as canvasmod


class Event:
    """A Tk-shaped event record."""

    __slots__ = ("x", "y", "keysym", "char", "delta", "num", "width",
                 "height", "state", "widget", "type")

    def __init__(self, x=0, y=0, keysym="", char="", delta=0, num=0,
                 width=0, height=0, state=0, widget=None, type=""):
        self.x = x
        self.y = y
        self.keysym = keysym
        self.char = char
        self.delta = delta
        self.num = num
        self.width = width
        self.height = height
        self.state = state
        self.widget = widget
        self.type = type

    def __repr__(self):
        return "<Event %s x=%d y=%d keysym=%r char=%r>" % (
            self.type, self.x, self.y, self.keysym, self.char)


class Window:
    """A headless top-level window: bindings, timers, and a canvas.

    Timers are a heap keyed by absolute deadline, which is what ``after()``
    means. The loop is single-threaded and re-entrant-safe in the one way
    that matters: a callback scheduling another ``after()`` cannot starve the
    frame, because only callbacks already due when the sweep began run in it.
    """

    def __init__(self, width=1000, height=720, title="FeetBrowser"):
        self.width = int(width)
        self.height = int(height)
        self._title = title
        self._bindings = {}
        self._timers = []
        self._cancelled = set()
        self._timer_seq = 0
        self._running = False
        self._destroyed = False
        self._clipboard = ""
        self.min_width = 0
        self.min_height = 0
        self.canvas = None
        self.children = []
        self.visible = True

    # -- Tk window API -----------------------------------------------------

    def title(self, text=None):
        if text is None:
            return self._title
        self._title = text
        self.on_title_changed(text)
        return None

    def geometry(self, spec=None):
        if spec is None:
            return "%dx%d" % (self.width, self.height)
        size = spec.split("+")[0].split("-")[0]
        if "x" in size:
            w, _, h = size.partition("x")
            if w.isdigit() and h.isdigit():
                self.resize(int(w), int(h))
        return None

    def minsize(self, width, height):
        self.min_width, self.min_height = int(width), int(height)

    def resizable(self, *_args):
        return None

    def lower(self, *_args):
        return None

    def lift(self, *_args):
        return None

    def focus_set(self):
        return None

    def withdraw(self):
        self.visible = False

    def deiconify(self):
        self.visible = True

    def protocol(self, _name=None, func=None):
        self._on_close = func

    def update_idletasks(self):
        self.flush_timers()

    def update(self):
        self.flush_timers()

    def destroy(self):
        if self._destroyed:
            return
        self._destroyed = True
        self._running = False
        for child in list(self.children):
            child.destroy()
        self.on_destroy()

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def winfo_exists(self):
        return not self._destroyed

    # -- clipboard ---------------------------------------------------------

    def clipboard_clear(self):
        self._clipboard = ""

    def clipboard_append(self, text):
        self._clipboard += text
        self.on_clipboard_set(self._clipboard)

    def clipboard_get(self):
        return self.on_clipboard_get()

    # -- bindings ----------------------------------------------------------

    def bind(self, sequence, func=None, add=None):
        if func is None:
            return self._bindings.get(sequence, [])
        if add:
            self._bindings.setdefault(sequence, []).append(func)
        else:
            self._bindings[sequence] = [func]
        return func

    def unbind(self, sequence, _funcid=None):
        self._bindings.pop(sequence, None)

    def dispatch(self, sequence, event=None):
        """Fire everything bound to `sequence`. Returns True if anything ran.

        Bindings that raise are reported and swallowed: one broken handler
        must not take down the loop, exactly as Tk's own report-and-continue
        behaviour guaranteed.
        """
        handlers = self._bindings.get(sequence)
        if not handlers:
            return False
        if event is None:
            event = Event(type=sequence)
        event.widget = self
        for func in list(handlers):
            try:
                func(event)
            except Exception as exc:  # noqa: BLE001 - parity with Tk
                self.on_callback_error(sequence, exc)
        return True

    # -- timers ------------------------------------------------------------

    def after(self, delay_ms, func=None, *args):
        """Schedule `func` after `delay_ms`. Returns a cancellable handle."""
        if func is None:
            deadline = time.monotonic() + delay_ms / 1000.0
            while time.monotonic() < deadline:
                self.flush_timers()
                time.sleep(0.001)
            return None
        self._timer_seq += 1
        handle = "after#%d" % self._timer_seq
        heapq.heappush(self._timers,
                       (time.monotonic() + delay_ms / 1000.0,
                        self._timer_seq, handle, func, args))
        return handle

    def after_idle(self, func, *args):
        return self.after(0, func, *args)

    def after_cancel(self, handle):
        if handle:
            self._cancelled.add(handle)

    def flush_timers(self):
        """Run every timer that is due. Returns seconds until the next one,
        or None when nothing is pending."""
        now = time.monotonic()
        due = []
        while self._timers and self._timers[0][0] <= now:
            _when, _seq, handle, func, args = heapq.heappop(self._timers)
            if handle in self._cancelled:
                self._cancelled.discard(handle)
                continue
            due.append((handle, func, args))
        for _handle, func, args in due:
            try:
                func(*args)
            except Exception as exc:  # noqa: BLE001 - parity with Tk
                self.on_callback_error("after", exc)
        if not self._timers:
            return None
        return max(0.0, self._timers[0][0] - time.monotonic())

    # -- loop --------------------------------------------------------------

    def mainloop(self):
        self._running = True
        while self._running and not self._destroyed:
            self.pump()
        self._running = False

    def quit(self):
        self._running = False

    def pump(self):
        """One iteration: deliver input, run timers, present a frame."""
        wait = self.flush_timers()
        if not self.poll_events():
            time.sleep(min(wait, 0.01) if wait is not None else 0.01)
        self.present()

    # -- hooks for platform subclasses ------------------------------------

    def poll_events(self):
        """Deliver pending platform input. True if anything was delivered."""
        return False

    def present(self):
        """Push the canvas surface to the screen."""

    def resize(self, width, height):
        self.width = max(self.min_width, int(width))
        self.height = max(self.min_height, int(height))
        if self.canvas is not None:
            self.canvas.resize(self.width, self.height)
        self.dispatch("<Configure>",
                      Event(width=self.width, height=self.height,
                            type="<Configure>"))

    def on_title_changed(self, title):
        """Platform windows update their titlebar here."""

    def on_destroy(self):
        """Platform windows tear down their native handle here."""

    def on_clipboard_set(self, text):
        """Platform windows push to the system clipboard here."""

    def on_clipboard_get(self):
        return self._clipboard

    def on_callback_error(self, where, exc):
        import traceback
        print("FeetBrowser: error in %s handler: %r" % (where, exc))
        traceback.print_exc()


class Tk(Window):
    """The root window. Owns the shared canvas the browser paints into."""

    def __init__(self, *_args, **kwargs):
        super().__init__(**kwargs)
        self.tk = None  # layout's batched-measure path checks for this


class Toplevel(Window):
    """A secondary window, used for popups."""

    def __init__(self, master=None, **kwargs):
        super().__init__(**kwargs)
        self.master = master
        if master is not None:
            master.children.append(self)

    def destroy(self):
        if self.master is not None and self in self.master.children:
            self.master.children.remove(self)
        super().destroy()


def make_canvas(window, width=None, height=None, bg="white", **kwargs):
    """Attach a canvas to `window` and return it."""
    surface = canvasmod.Canvas(window,
                               width=width or window.width,
                               height=height or window.height,
                               bg=bg, **kwargs)
    window.canvas = surface
    return surface
