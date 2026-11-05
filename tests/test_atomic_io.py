import unittest
from unittest.mock import patch
from src.core.atomic_io import replace_file


class TestAtomicIO(unittest.TestCase):
    @patch('src.core.atomic_io.time.sleep')
    def test_transient_permission_errors_retry_without_truncating_target(self,sleep):
        with patch('src.core.atomic_io.Path.replace',side_effect=[PermissionError(),PermissionError(),None]) as replace:
            replace_file('source.tmp','target.json')
        self.assertEqual(replace.call_count,3);self.assertEqual(sleep.call_count,2)

    @patch('src.core.atomic_io.time.sleep')
    def test_persistent_errors_are_not_suppressed(self,sleep):
        with patch('src.core.atomic_io.Path.replace',side_effect=PermissionError()) as replace:
            with self.assertRaises(PermissionError):replace_file('source.tmp','target.json')
        self.assertEqual(replace.call_count,6)

    @patch('src.core.atomic_io.time.sleep')
    def test_other_io_errors_do_not_retry(self,sleep):
        with patch('src.core.atomic_io.Path.replace',side_effect=FileNotFoundError()):
            with self.assertRaises(FileNotFoundError):replace_file('source.tmp','target.json')
        sleep.assert_not_called()
