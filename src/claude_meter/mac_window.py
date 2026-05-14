"""macOS NSWindow shim — pin a Qt widget above every app on every space.

Lifted directly from curby's overlay shim (Documents/Dev/curby/src/mac_window.py),
which is battle-tested. The key is calling objc.objc_object on the actual NSView
pointer returned by widget.winId() — NOT iterating NSApp.windows() and matching
by windowNumber (that was the bug in the previous attempt).
"""
from __future__ import annotations

import ctypes
import sys


_LEVEL_FLOATING = 3
_LEVEL_STATUS_BAR = 25
_LEVEL_POPUP_MENU = 101
_LEVEL_SCREEN_SAVER = 1000

_BEHAVIOR_CAN_JOIN_ALL_SPACES = 1 << 0
_BEHAVIOR_STATIONARY = 1 << 4
_BEHAVIOR_FULLSCREEN_AUXILIARY = 1 << 8
_BEHAVIOR_IGNORES_CYCLE = 1 << 6


def make_always_visible(widget) -> bool:
    """Pin a Qt widget so it floats above every app on every space.

    Returns True on success. Must be called AFTER widget.show() so winId
    points to a real native NSView. Safe to call repeatedly.
    """
    if sys.platform != "darwin":
        return False
    try:
        import objc

        nsview_ptr = int(widget.winId())
        if not nsview_ptr:
            print("[mac] make_always_visible: widget has no native handle yet")
            return False
        nsview = objc.objc_object(c_void_p=ctypes.c_void_p(nsview_ptr))
        nswindow = nsview.window()
        if nswindow is None:
            print("[mac] make_always_visible: NSView has no window")
            return False
        nswindow.setLevel_(_LEVEL_STATUS_BAR)
        nswindow.setCollectionBehavior_(
            _BEHAVIOR_CAN_JOIN_ALL_SPACES
            | _BEHAVIOR_STATIONARY
            | _BEHAVIOR_FULLSCREEN_AUXILIARY
            | _BEHAVIOR_IGNORES_CYCLE
        )
        try:
            nswindow.setHidesOnDeactivate_(False)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[mac] make_always_visible failed: {e}")
        return False
