import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from src.core.controller import FGOController
from src.core.device_manager import DeviceManager
from src.server.web_server import GlobalState


class TestLivePreview(unittest.TestCase):
    def test_offline_mumu_port_does_not_outrank_online_emulator(self):
        manager = DeviceManager.__new__(DeviceManager)
        manager.run_adb = Mock(side_effect=[
            (0, 'List of devices attached\n127.0.0.1:16416 offline\n'
                '127.0.0.1:16417 device model:MuMu\n'),
            (0, 'Physical size: 1920x1080'),
        ])
        devices = manager.get_connected_devices(auto_connect=False)
        self.assertEqual([d.serial for d in devices], ['127.0.0.1:16417', '127.0.0.1:16416'])

    def test_preview_replaces_cached_offline_controller(self):
        state = GlobalState.__new__(GlobalState)
        state._preview_lock = threading.Lock()
        state._controller = Mock(device_serial='127.0.0.1:16416')
        state.device_mgr = Mock(adb_path='mumu-adb', **{'run_adb.return_value': (1, 'offline')})
        with patch('src.server.web_server.FGOController') as controller:
            self.assertIs(state.capture_preview(), controller.return_value.screencap.return_value)
        controller.assert_called_once_with(adb_path='mumu-adb', log_callback=state.add_log)
        controller.return_value.screencap.assert_called_once_with(require_capture=True)

    def test_failed_capture_is_not_returned_as_a_valid_preview_frame(self):
        with patch.object(FGOController, '_get_screen_size', return_value=(32, 32)):
            ctrl = FGOController(device_serial='offline')
        failed = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'error: device offline')
        with patch.object(ctrl, '_run_cmd', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, '无法从 offline 获取截图'):
                ctrl.screencap(require_capture=True)
            self.assertEqual(ctrl.screencap().getpixel((0, 0)), (0, 0, 0, 255))
