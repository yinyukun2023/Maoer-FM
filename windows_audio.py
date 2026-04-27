from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, c_int, c_ulong, c_void_p, c_wchar_p, wintypes
from functools import lru_cache
from typing import Iterable


MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def debug_log(message: str) -> None:
    if os.environ.get("MAOER_DEBUG"):
        print(f"[audio] {message}", flush=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if kernel32 is not None:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def set_current_app_volume(volume: int | float, include_process_names: Iterable[str] = ()) -> bool:
    if os.name != "nt":
        debug_log(f"skip non-windows volume={volume}")
        return False

    level = max(0.0, min(1.0, float(volume) / 100.0))
    target_pids, names = _target_processes(include_process_names)
    debug_log(f"set_volume requested={volume} level={level:.2f} target_pids={sorted(target_pids)} names={sorted(names)}")
    if not target_pids and not names:
        debug_log("set_volume no targets")
        return False

    try:
        objects = _audio_interfaces()
    except Exception as exc:
        debug_log(f"set_volume audio_interfaces failed: {type(exc).__name__}: {exc}")
        return False

    comtypes = objects["comtypes"]
    try:
        comtypes.CoInitialize()
        enumerator = comtypes.CoCreateInstance(
            objects["CLSID_MMDeviceEnumerator"],
            objects["IMMDeviceEnumerator"],
            objects["CLSCTX_ALL"],
        )
        device = enumerator.GetDefaultAudioEndpoint(0, 1)
        manager = device.Activate(
            objects["IAudioSessionManager2"]._iid_,
            objects["CLSCTX_ALL"],
            None,
        ).QueryInterface(objects["IAudioSessionManager2"])
        session_enum = manager.GetSessionEnumerator()
        count = session_enum.GetCount()
    except Exception as exc:
        debug_log(f"set_volume enumerate failed: {type(exc).__name__}: {exc}")
        return False

    table = _process_table()
    sessions = []
    for index in range(count):
        try:
            session = session_enum.GetSession(index)
            control2 = session.QueryInterface(objects["IAudioSessionControl2"])
            pid = int(control2.GetProcessId())
            metadata = _session_metadata(session, control2)
        except Exception as exc:
            debug_log(f"session index={index} read failed: {type(exc).__name__}: {exc}")
            continue
        process_name = table.get(pid, (0, ""))[1]
        debug_log(
            f"session index={index} pid={pid} process={process_name or '<unknown>'} "
            f"metadata={metadata[:180]!r}"
        )
        sessions.append((pid, process_name, metadata, session))

    changed = _set_session_volumes(sessions, target_pids, names, objects, level)
    debug_log(f"set_volume changed={changed}")
    return changed


def _set_session_volumes(
    sessions: list[tuple[int, str, str, object]],
    target_pids: set[int],
    names: set[str],
    objects: dict[str, object],
    level: float,
) -> bool:
    changed = False
    for pid, process_name, metadata, session in sessions:
        if not _is_target_session(pid, metadata, target_pids, names):
            continue
        try:
            volume_control = session.QueryInterface(objects["ISimpleAudioVolume"])
            volume_control.SetMute(False, None)
            volume_control.SetMasterVolume(level, None)
            debug_log(f"matched pid={pid} process={process_name or '<unknown>'} level={level:.2f}")
            changed = True
        except Exception as exc:
            debug_log(f"matched pid={pid} set failed: {type(exc).__name__}: {exc}")
            continue
    return changed


def _session_metadata(session: object, control2: object) -> str:
    parts: list[str] = []
    for getter in (
        getattr(session, "GetDisplayName", None),
        getattr(session, "GetIconPath", None),
        getattr(control2, "GetSessionIdentifier", None),
        getattr(control2, "GetSessionInstanceIdentifier", None),
    ):
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:
            continue
        if value:
            parts.append(str(value).lower())
    return "\n".join(parts)


def _is_target_session(pid: int, metadata: str, target_pids: set[int], names: set[str]) -> bool:
    if pid in target_pids:
        return True
    if not names:
        return False
    table = _process_table()
    process = table.get(pid)
    if process and process[1].lower() in names:
        return True
    return any(name in metadata for name in names)


def _target_processes(include_process_names: Iterable[str]) -> tuple[set[int], set[str]]:
    current_pid = os.getpid()
    table = _process_table()
    target_pids = {current_pid}

    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, _name) in table.items():
            if pid not in target_pids and parent_pid in target_pids:
                target_pids.add(pid)
                changed = True

    names = {name.lower() for name in include_process_names if name}
    return target_pids, names


def _process_table() -> dict[int, tuple[int, str]]:
    if kernel32 is None:
        return {}

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return {}

    table: dict[int, tuple[int, str]] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return table
        while True:
            table[int(entry.th32ProcessID)] = (
                int(entry.th32ParentProcessID),
                str(entry.szExeFile).lower(),
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return table


@lru_cache(maxsize=1)
def _audio_interfaces() -> dict[str, object]:
    import comtypes
    from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown

    DWORD = c_ulong
    BOOL = c_int

    class IMMDevice(IUnknown):
        pass

    class IMMDeviceEnumerator(IUnknown):
        pass

    class IAudioSessionEnumerator(IUnknown):
        pass

    class IAudioSessionControl(IUnknown):
        pass

    class IAudioSessionControl2(IAudioSessionControl):
        pass

    class IAudioSessionManager2(IUnknown):
        pass

    class ISimpleAudioVolume(IUnknown):
        pass

    IMMDevice._iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    IMMDevice._methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "Activate",
            (["in"], POINTER(GUID), "iid"),
            (["in"], DWORD, "dwClsCtx"),
            (["in"], c_void_p, "pActivationParams"),
            (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
        ),
    ]

    IMMDeviceEnumerator._iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    IMMDeviceEnumerator._methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "EnumAudioEndpoints",
            (["in"], c_int, "dataFlow"),
            (["in"], DWORD, "dwStateMask"),
            (["out"], POINTER(c_void_p), "ppDevices"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDefaultAudioEndpoint",
            (["in"], c_int, "dataFlow"),
            (["in"], c_int, "role"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
        ),
    ]

    IAudioSessionEnumerator._iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    IAudioSessionEnumerator._methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
        COMMETHOD(
            [],
            HRESULT,
            "GetSession",
            (["in"], c_int, "SessionCount"),
            (["out"], POINTER(POINTER(IAudioSessionControl)), "Session"),
        ),
    ]

    IAudioSessionManager2._iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    IAudioSessionManager2._methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "GetAudioSessionControl",
            (["in"], c_void_p, "AudioSessionGuid"),
            (["in"], DWORD, "StreamFlags"),
            (["out"], POINTER(c_void_p), "SessionControl"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetSimpleAudioVolume",
            (["in"], c_void_p, "AudioSessionGuid"),
            (["in"], DWORD, "StreamFlags"),
            (["out"], POINTER(c_void_p), "AudioVolume"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetSessionEnumerator",
            (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum"),
        ),
    ]

    IAudioSessionControl._iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
    IAudioSessionControl._methods_ = [
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD(
            [],
            HRESULT,
            "SetDisplayName",
            (["in"], c_wchar_p, "Value"),
            (["in"], POINTER(GUID), "EventContext"),
        ),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD(
            [],
            HRESULT,
            "SetIconPath",
            (["in"], c_wchar_p, "Value"),
            (["in"], POINTER(GUID), "EventContext"),
        ),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
        COMMETHOD(
            [],
            HRESULT,
            "SetGroupingParam",
            (["in"], POINTER(GUID), "Override"),
            (["in"], POINTER(GUID), "EventContext"),
        ),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "NewNotifications")),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "NewNotifications")),
    ]

    IAudioSessionControl2._iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
    IAudioSessionControl2._methods_ = [
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "pRetVal")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(DWORD), "pRetVal")),
        COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
        COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], BOOL, "optOut")),
    ]

    ISimpleAudioVolume._iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
    ISimpleAudioVolume._methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "SetMasterVolume",
            (["in"], ctypes.c_float, "fLevel"),
            (["in"], POINTER(GUID), "EventContext"),
        ),
        COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(ctypes.c_float), "pfLevel")),
        COMMETHOD(
            [],
            HRESULT,
            "SetMute",
            (["in"], BOOL, "bMute"),
            (["in"], POINTER(GUID), "EventContext"),
        ),
        COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
    ]

    return {
        "comtypes": comtypes,
        "CLSCTX_ALL": CLSCTX_ALL,
        "CLSID_MMDeviceEnumerator": GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        "IMMDeviceEnumerator": IMMDeviceEnumerator,
        "IAudioSessionControl2": IAudioSessionControl2,
        "IAudioSessionManager2": IAudioSessionManager2,
        "ISimpleAudioVolume": ISimpleAudioVolume,
    }
