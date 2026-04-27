from __future__ import annotations

import ctypes
import os
from functools import lru_cache
from typing import Any


EVENT_OBJECT_LIVEREGIONCHANGED = 0x8019
OBJID_CLIENT_DWORD = ctypes.c_ulong(-4).value
OBJID_CLIENT_LONG = -4
CHILDID_SELF = 0
LIVE_SETTING_ASSERTIVE = 2


def debug_log(message: str) -> None:
    if os.environ.get("MAOER_DEBUG"):
        print(f"[uia] {message}", flush=True)


class ScreenReaderAnnouncer:
    def __init__(self, live_region: Any) -> None:
        self.live_region = live_region
        self._live_region_ready = False
        self._failed = os.name != "nt"

    def announce(self, message: str) -> bool:
        text = " ".join((message or "").split())
        if not text or self._failed:
            return False

        try:
            handle = self._handle()
            self._set_accessible_text(text)
            self._ensure_live_region(handle)
            objects = _automation_objects()
            objects["user32"].NotifyWinEvent(
                EVENT_OBJECT_LIVEREGIONCHANGED,
                handle,
                OBJID_CLIENT_LONG,
                CHILDID_SELF,
            )
            return True
        except Exception as exc:
            debug_log(f"announce failed: {type(exc).__name__}: {exc}")
            self._failed = True
            return False

    def _handle(self) -> int:
        handle = int(self.live_region.GetHandle())
        if not handle:
            raise RuntimeError("live region has no native window handle")
        return handle

    def _set_accessible_text(self, text: str) -> None:
        if hasattr(self.live_region, "SetLabel"):
            self.live_region.SetLabel(text)
        if hasattr(self.live_region, "SetName"):
            self.live_region.SetName(text)

    def _ensure_live_region(self, handle: int) -> None:
        if self._live_region_ready:
            return

        objects = _automation_objects()
        objects["comtypes"].CoInitialize()
        service = objects["comtypes"].CoCreateInstance(
            objects["CLSID_AccPropServices"],
            objects["IAccPropServices"],
            objects["CLSCTX_INPROC_SERVER"],
        )
        service.SetHwndProp(
            handle,
            OBJID_CLIENT_DWORD,
            CHILDID_SELF,
            objects["LiveSetting_Property_GUID"],
            objects["VARIANT"](LIVE_SETTING_ASSERTIVE),
        )
        self._live_region_ready = True


@lru_cache(maxsize=1)
def _automation_objects() -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("UI Automation live regions are only available on Windows")

    import comtypes
    from comtypes import CLSCTX_INPROC_SERVER, COMMETHOD, GUID, HRESULT, IUnknown
    from comtypes.automation import VARIANT
    from ctypes import POINTER, c_int, c_void_p, wintypes

    byte = ctypes.c_ubyte
    dword = wintypes.DWORD
    hwnd = wintypes.HWND

    class IAccPropServices(IUnknown):
        pass

    IAccPropServices._iid_ = GUID("{6E26E776-04F0-495D-80E4-3330352E3169}")
    IAccPropServices._methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "SetPropValue",
            (["in"], POINTER(byte), "pIDString"),
            (["in"], dword, "dwIDStringLen"),
            (["in"], GUID, "idProp"),
            (["in"], VARIANT, "var"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetPropServer",
            (["in"], POINTER(byte), "pIDString"),
            (["in"], dword, "dwIDStringLen"),
            (["in"], POINTER(GUID), "paProps"),
            (["in"], c_int, "cProps"),
            (["in"], c_void_p, "pServer"),
            (["in"], c_int, "annoScope"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "ClearProps",
            (["in"], POINTER(byte), "pIDString"),
            (["in"], dword, "dwIDStringLen"),
            (["in"], POINTER(GUID), "paProps"),
            (["in"], c_int, "cProps"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetHwndProp",
            (["in"], hwnd, "hwnd"),
            (["in"], dword, "idObject"),
            (["in"], dword, "idChild"),
            (["in"], GUID, "idProp"),
            (["in"], VARIANT, "var"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetHwndPropStr",
            (["in"], hwnd, "hwnd"),
            (["in"], dword, "idObject"),
            (["in"], dword, "idChild"),
            (["in"], GUID, "idProp"),
            (["in"], wintypes.LPCWSTR, "str"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetHwndPropServer",
            (["in"], hwnd, "hwnd"),
            (["in"], dword, "idObject"),
            (["in"], dword, "idChild"),
            (["in"], POINTER(GUID), "paProps"),
            (["in"], c_int, "cProps"),
            (["in"], c_void_p, "pServer"),
            (["in"], c_int, "annoScope"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "ClearHwndProps",
            (["in"], hwnd, "hwnd"),
            (["in"], dword, "idObject"),
            (["in"], dword, "idChild"),
            (["in"], POINTER(GUID), "paProps"),
            (["in"], c_int, "cProps"),
        ),
    ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.NotifyWinEvent.argtypes = [wintypes.DWORD, hwnd, wintypes.LONG, wintypes.LONG]
    user32.NotifyWinEvent.restype = None

    return {
        "comtypes": comtypes,
        "CLSCTX_INPROC_SERVER": CLSCTX_INPROC_SERVER,
        "CLSID_AccPropServices": GUID("{B5F8350B-0548-48B1-A6EE-88BD00B4A5E7}"),
        "IAccPropServices": IAccPropServices,
        "LiveSetting_Property_GUID": GUID("{C12BCD8E-2A8E-4950-8AE7-3625111D58EB}"),
        "VARIANT": VARIANT,
        "user32": user32,
    }
