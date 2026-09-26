import os
import threading
import unittest
from unittest.mock import Mock, patch

from audio_output import AudioOutputRouter, OutputDevice, SYSTEM_OUTPUT, route_player_output
from windows_audio import _set_device_volume


class OutputRouterTests(unittest.TestCase):
    def setUp(self):
        self.devices = (SYSTEM_OUTPUT, OutputDevice("a", "声卡 A"), OutputDevice("b", "声卡 B"))
        self.router = AudioOutputRouter()
        self.addCleanup(self.router.close)
        for target, value in (("list_output_devices", self.devices), ("player_audio_sessions", {42: {"a"}})):
            patcher = patch("audio_output." + target, return_value=value)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        patcher = patch("audio_output.route_player_output")
        self.route = patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, method, *args):
        event = threading.Event()
        results = []
        def done(result):
            results.append(result)
            event.set()
        method(*args, done)
        self.assertTrue(event.wait(3), "audio worker failed to complete")
        return results[0]

    def test_forward_backward_cycles_include_system_and_wrap(self):
        for direction, expected in ((1, "a"), (1, "b"), (1, ""), (-1, "b"), (-1, "a"), (-1, "")):
            result = self.request(self.router.cycle, direction)
            self.assertTrue(result["ok"])
            self.assertEqual(result["device_id"], expected)
            self.route.assert_called_with(expected, {42})

    def test_enumeration_and_routing_run_off_calling_thread(self):
        self.route.side_effect = lambda *_args: self.assertIsNot(threading.current_thread(), threading.main_thread())
        self.assertTrue(self.request(self.router.select, "a")["ok"])

    def test_failed_switch_does_not_advance_selected_device(self):
        self.route.side_effect = OSError("设备离线")
        result = self.request(self.router.cycle, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(self.router.device_id, "")
        self.assertIn("设备离线", result["error"])

    def test_default_can_be_saved_before_playback_but_cycle_requires_audio(self):
        self.player_audio_sessions.return_value = {}
        self.assertTrue(self.request(self.router.select, "b")["ok"])
        self.route.assert_not_called()
        self.assertFalse(self.request(self.router.cycle, 1)["ok"])
        self.player_audio_sessions.return_value = {42: {"a"}}
        self.assertTrue(self.request(self.router.poll)["ok"])
        self.route.assert_called_once_with("b", {42})

    def test_poll_handles_new_process_and_disconnected_device_without_repeated_writes(self):
        self.request(self.router.select, "b")
        self.request(self.router.poll)
        self.route.assert_called_once()
        self.player_audio_sessions.return_value = {55: {"a"}}
        self.request(self.router.poll)
        self.route.assert_called_with("b", {55})
        self.list_output_devices.return_value = self.devices[:2]
        result = self.request(self.router.poll)
        self.assertTrue(result["fallback"])
        self.assertEqual(self.router.device_id, "")
        self.route.assert_called_with("", {55})
        self.assertFalse(self.request(self.router.poll)["fallback"])

    def test_missing_device_is_not_silently_selected(self):
        self.assertFalse(self.request(self.router.select, "missing")["ok"])
        self.route.assert_not_called()

    def test_rapid_requests_remain_serial_and_each_uses_previous_selection(self):
        finished = threading.Event()
        result = []
        for _ in range(4):
            self.router.cycle(1, result.append)
        self.router.refresh(lambda _result: finished.set())
        self.assertTrue(finished.wait(3))
        self.assertEqual([value["device_id"] for value in result], ["a", "b", "", "a"])


class NativeRoutingScopeTests(unittest.TestCase):
    def test_foreign_webview_and_current_speech_process_are_never_routed(self):
        with patch("audio_output._target_processes", return_value=({os.getpid(), 42}, set())), \
             patch("audio_output._process_table", return_value={42: (os.getpid(), "msedgewebview2.exe"),
                                                                77: (10, "msedgewebview2.exe")}), \
             patch("audio_output._Policy") as policy:
            for pids in ({77}, {os.getpid()}, set(), {42, 77}):
                with self.assertRaises(OSError):
                    route_player_output("a", pids)
            policy.assert_not_called()

    def test_failed_partial_route_rolls_back_previous_per_process_policy(self):
        policy = Mock()
        policy.get.side_effect = ["old0", "old1", "new"]
        with patch("audio_output._target_processes", return_value=({42}, set())), \
             patch("audio_output._process_table", return_value={42: (os.getpid(), "msedgewebview2.exe")}), \
             patch("audio_output._core_audio"), patch("audio_output._Policy") as policy_type:
            policy_type.return_value.__enter__.return_value = policy
            with self.assertRaises(OSError):
                route_player_output("a", {42})
        self.assertEqual(policy.set.call_args_list[-2].args, (42, 0, "old0"))
        self.assertEqual(policy.set.call_args_list[-1].args, (42, 1, "old1"))

    def test_volume_follows_sessions_on_non_default_device_and_excludes_other_apps(self):
        objects = {name: Mock() for name in ("comtypes", "CLSID_MMDeviceEnumerator", "IMMDeviceEnumerator",
                                            "CLSCTX_ALL", "IAudioSessionManager2", "IAudioSessionControl2", "ISimpleAudioVolume")}
        enumerator = objects["comtypes"].CoCreateInstance.return_value
        enumerator.EnumAudioEndpoints.return_value.GetCount.return_value = 2
        devices = [Mock(), Mock()]
        enumerator.EnumAudioEndpoints.return_value.Item.side_effect = devices
        sessions = [Mock(), Mock()]
        for device, session, pid in zip(devices, sessions, (99, 42)):
            listing = device.Activate.return_value.QueryInterface.return_value.GetSessionEnumerator.return_value
            listing.GetCount.return_value = 1
            listing.GetSession.return_value = session
            session.QueryInterface.return_value.GetProcessId.return_value = pid
        with patch("windows_audio._process_table", return_value={}), patch("windows_audio._session_metadata", return_value=""):
            self.assertTrue(_set_device_volume(objects, {42}, set(), 0.4))
        sessions[0].QueryInterface.return_value.SetMasterVolume.assert_not_called()
        sessions[1].QueryInterface.return_value.SetMasterVolume.assert_called_once_with(0.4, None)

