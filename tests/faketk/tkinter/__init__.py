"""A minimal stand-in for tkinter, so that tests can drive gui.py headless
(no display, and no _tkinter on many CI images). Only what gui.py uses."""

import queue
import time

END = "end"
NORMAL = "normal"
DISABLED = "disabled"
LEFT = "left"
RIGHT = "right"
X = "x"
Y = "y"
BOTH = "both"
N = "n"
W = "w"
WORD = "word"
TkVersion = 0.0


class Variable:
    def __init__(self, master=None, value=None):
        self._v = value if value is not None else ""
        self._traces = []

    def get(self):
        return self._v

    def set(self, v):
        self._v = v
        for cb in self._traces:
            cb()

    def trace_add(self, mode, cb):
        self._traces.append(cb)


class StringVar(Variable):
    pass


class IntVar(Variable):
    def __init__(self, master=None, value=0):
        super().__init__(master, value)


class Widget:
    """Any method call is accepted and recorded in self.calls."""

    def __init__(self, *a, **kw):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*a, **kw):
            self.calls.append((name, a, kw))

        return method


class Listbox(Widget):
    def __init__(self, *a, **kw):
        super().__init__()
        self.items = []

    def get(self, first, last=None):
        return tuple(self.items)

    def insert(self, index, value):
        self.items.append(value)

    def delete(self, first, last=None):
        if last is None:
            del self.items[first]
        else:
            self.items.clear()

    def curselection(self):
        return ()


class Text(Widget):
    def __init__(self, *a, **kw):
        super().__init__()
        self.text = ""

    def insert(self, index, text):
        self.text += text


class Tk(Widget):
    def __init__(self, *a, **kw):
        super().__init__()
        self._after = queue.Queue()
        self.protocols = {}
        self.destroyed = False

    def after(self, ms, fn=None, *args):
        self._after.put(lambda: fn(*args))
        return "after#"

    def protocol(self, name, fn):
        self.protocols[name] = fn

    def destroy(self):
        self.destroyed = True

    def register(self, fn):
        return "cmd"

    def pump_until(self, done, timeout=20.0):
        """Run after-callbacks, as the main loop would, until done() or timeout."""
        deadline = time.monotonic() + timeout
        while not done():
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError("GUI did not finish in time")
            try:
                fn = self._after.get(timeout=min(left, 0.05))
            except queue.Empty:
                continue
            fn()
