"""Windows render endpoints and player-only routing (never the system default).

The versioned internal AudioPolicyConfig ABI is also used by EarTrumpet:
https://github.com/File-New-Project/EarTrumpet/tree/master/EarTrumpet/Interop/MMDeviceAPI
Unsupported Windows versions fail explicitly instead of changing global audio.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import ctypes
from contextlib import contextmanager
from functools import lru_cache
import os
import threading

from windows_audio import _audio_interfaces, _process_table, _target_processes


@dataclass(frozen=True)
class OutputDevice:
    id: str
    name: str


SYSTEM_OUTPUT = OutputDevice("", "跟随系统")


@contextmanager
def _core_audio():
    objects = _audio_interfaces()
    com = objects["comtypes"]
    com.CoInitialize()
    enumerator = None
    try:
        enumerator = com.CoCreateInstance(objects["CLSID_MMDeviceEnumerator"],
                                         objects["IMMDeviceEnumerator"], objects["CLSCTX_ALL"])
        yield objects, enumerator
    finally:
        # Release interfaces on their owning COM apartment before uninitializing.
        enumerator = None
        com.CoUninitialize()


@lru_cache(maxsize=1)
def _property_types():
    from comtypes import IUnknown, GUID, COMMETHOD, HRESULT
    class Key(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]
    class Array(ctypes.Structure):
        _fields_ = [("count", ctypes.c_ulong), ("data", ctypes.c_void_p)]
    class Value(ctypes.Union):
        _fields_ = [("text", ctypes.c_wchar_p), ("array", Array), ("number", ctypes.c_int64)]
    class Variant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("value", Value)]
    class Store(IUnknown):
        _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], ctypes.POINTER(ctypes.c_ulong), "count")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], ctypes.c_ulong, "index"), (["out"], ctypes.POINTER(Key), "key")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], ctypes.POINTER(Key), "key"),
                      (["out"], ctypes.POINTER(Variant), "value")),
        ]
    return Store, Key(GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)


def _device_name(device) -> str:
    store_type, key = _property_types()
    store = device.OpenPropertyStore(0).QueryInterface(store_type)
    value = store.GetValue(ctypes.byref(key))
    try:
        return str(value.value.text) if value.vt == 31 and value.value.text else device.GetId()
    finally:
        ctypes.OleDLL("ole32").PropVariantClear(ctypes.byref(value))


def list_output_devices() -> tuple[OutputDevice, ...]:
    with _core_audio() as (_objects, enumerator):
        endpoints = enumerator.EnumAudioEndpoints(0, 1)  # render, active only
        devices = []
        for index in range(endpoints.GetCount()):
            device = endpoints.Item(index)
            devices.append(OutputDevice(device.GetId(), _device_name(device)))
        device = endpoints = None
    return (SYSTEM_OUTPUT, *sorted(devices, key=lambda device: (device.name.casefold(), device.id)))


def player_audio_sessions(active_only: bool = False) -> dict[int, set[str]]:
    """Only audio sessions owned by this process's WebView descendants."""
    descendants, _names = _target_processes(())
    table = _process_table()
    targets = {pid for pid in descendants if pid != os.getpid()
               and table.get(pid, (0, ""))[1] == "msedgewebview2.exe"}
    sessions: dict[int, set[str]] = {}
    with _core_audio() as (objects, enumerator):
        endpoints = enumerator.EnumAudioEndpoints(0, 1)
        for index in range(endpoints.GetCount()):
            device = endpoints.Item(index)
            try:
                manager = device.Activate(objects["IAudioSessionManager2"]._iid_, objects["CLSCTX_ALL"], None).QueryInterface(objects["IAudioSessionManager2"])
                listing = manager.GetSessionEnumerator()
                for slot in range(listing.GetCount()):
                    session = listing.GetSession(slot)
                    control = session.QueryInterface(objects["IAudioSessionControl2"])
                    pid = int(control.GetProcessId())
                    state = session.GetState()
                    if pid in targets and state != 2 and (not active_only or state == 1):
                        sessions.setdefault(pid, set()).add(device.GetId())
                control = session = listing = manager = None
            except OSError:
                continue  # device was disconnected during enumeration
        device = endpoints = None
    return sessions


@lru_cache(maxsize=1)
def _policy_library():
    dll = ctypes.WinDLL("combase")
    ptr = ctypes.c_void_p
    dll.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ptr)]
    dll.WindowsCreateString.restype = ctypes.c_long
    dll.WindowsDeleteString.argtypes = [ptr]
    dll.WindowsDeleteString.restype = ctypes.c_long
    dll.WindowsGetStringRawBuffer.argtypes = [ptr, ctypes.POINTER(ctypes.c_uint32)]
    dll.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p
    dll.RoGetActivationFactory.argtypes = [ptr, ptr, ctypes.POINTER(ptr)]
    dll.RoGetActivationFactory.restype = ctypes.c_long
    return dll


def _check_hr(result: int) -> None:
    if result < 0:
        raise OSError(f"音频设备接口失败（0x{result & 0xffffffff:08X}）")


@contextmanager
def _hstring(text: str):
    handle = ctypes.c_void_p()
    dll = _policy_library()
    _check_hr(dll.WindowsCreateString(text, len(text.encode("utf-16-le")) // 2, ctypes.byref(handle)))
    try:
        yield handle
    finally:
        dll.WindowsDeleteString(handle)


class _Policy:
    """Apartment-local WinRT factory. No global endpoint setters exposed."""
    def __enter__(self):
        from comtypes import GUID
        dll = _policy_library()
        self.ptr = ctypes.c_void_p()
        with _hstring("Windows.Media.Internal.AudioPolicyConfig") as name:
            for iid in ("ab3d4648-e242-459f-b02f-541c70306324", "2a59116d-6c4f-45e0-a74f-707e3fef9258"):
                result = dll.RoGetActivationFactory(name, ctypes.byref(GUID("{" + iid + "}")), ctypes.byref(self.ptr))
                if result >= 0:
                    break
            _check_hr(result)
        return self

    def _method(self, slot, restype, *args):
        vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *args)(vtable[slot])

    def get(self, pid: int, role: int) -> str:
        value = ctypes.c_void_p()
        # IInspectable (6 slots), 19 reserved methods, then Set/Get.
        fn = self._method(26, ctypes.c_long, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
        _check_hr(fn(self.ptr, pid, 0, role, ctypes.byref(value)))
        try:
            return _policy_library().WindowsGetStringRawBuffer(value, None) or ""
        finally:
            _policy_library().WindowsDeleteString(value)

    def set(self, pid: int, role: int, full_id: str) -> None:
        fn = self._method(25, ctypes.c_long, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
        with _hstring(full_id) as value:
            _check_hr(fn(self.ptr, pid, 0, role, value))

    def __exit__(self, *_args):
        self._method(2, ctypes.c_ulong)(self.ptr)
        self.ptr = None


def route_player_output(device_id: str, pids: set[int]) -> None:
    full_id = "\\\\?\\SWD#MMDEVAPI#" + device_id + "#{e6327cad-dcec-4949-ae8a-991e976a79d2}" if device_id else ""
    # Validate scope again immediately before writing. Never match by name alone.
    descendants, _ = _target_processes(())
    table = _process_table()
    if not pids or any(pid == os.getpid() or pid not in descendants
                       or table.get(pid, (0, ""))[1] != "msedgewebview2.exe" for pid in pids):
        raise OSError("播放进程已变化，请稍后重试")
    with _core_audio(), _Policy() as policy:
        previous = {(pid, role): policy.get(pid, role) for pid in pids for role in (0, 1)}
        try:
            for pid, role in previous:
                policy.set(pid, role, full_id)
                if policy.get(pid, role).casefold() != full_id.casefold():
                    raise OSError("系统未确认输出设备切换")
        except Exception:
            for (pid, role), old in previous.items():
                try:
                    policy.set(pid, role, old)
                except OSError:
                    pass
            raise


class AudioOutputRouter:
    """Serial background operations: COM/driver calls never block the UI."""
    def __init__(self, device_id: str = ""):
        self.device_id = device_id
        self.devices = (SYSTEM_OUTPUT,)
        self._applied: dict[int, str] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audio-output")
        self._closed = False
        self._poll_pending = False
        self._lock = threading.Lock()

    def _submit(self, operation, callback=None):
        if self._closed:
            return
        def work():
            try:
                result = operation()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if callback and not self._closed:
                callback(result)
        self._executor.submit(work)

    def refresh(self, callback=None):
        def operation():
            self.devices = list_output_devices()
            return {"ok": True, "devices": self.devices}
        self._submit(operation, callback)

    def _apply(self, device: OutputDevice, require_session: bool):
        sessions = player_audio_sessions()
        if require_session and not sessions:
            raise OSError("播放器尚未就绪，请开始播放后再切换输出设备")
        pending = {pid for pid in sessions if self._applied.get(pid) != device.id}
        if pending:
            route_player_output(device.id, pending)
        self._applied = {pid: device.id for pid in sessions}
        self.device_id = device.id
        return {"ok": True, "device_id": device.id, "name": device.name}

    def select(self, device_id: str, callback=None):
        def operation():
            self.devices = list_output_devices()
            device = next((item for item in self.devices if item.id == device_id), None)
            if device is None:
                raise OSError("所选播放设备已不可用，请重新选择")
            return self._apply(device, False)
        self._submit(operation, callback)

    def cycle(self, direction: int, callback):
        def operation():
            self.devices = list_output_devices()
            index = next((i for i, item in enumerate(self.devices) if item.id == self.device_id), 0)
            return self._apply(self.devices[(index + direction) % len(self.devices)], True)
        self._submit(operation, callback)

    def poll(self, callback=None):
        with self._lock:
            if self._poll_pending or self._closed:
                return
            self._poll_pending = True
        def operation():
            try:
                self.devices = list_output_devices()
                device = next((item for item in self.devices if item.id == self.device_id), SYSTEM_OUTPUT)
                fallback = device.id != self.device_id
                result = self._apply(device, False)
                result["fallback"] = fallback
                return result
            finally:
                with self._lock:
                    self._poll_pending = False
        self._submit(operation, callback)

    def close(self):
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
