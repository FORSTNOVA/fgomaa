import threading
import unittest
from unittest.mock import Mock, patch

from src.server import web_server


class TestLogPolling(unittest.TestCase):
    def test_log_cursor_survives_buffer_rollover_and_reconnect(self):
        state = web_server.GlobalState.__new__(web_server.GlobalState)
        state.logs = []
        state.log_cursor = 0
        state.log_lock = threading.Lock()
        state.is_running = True
        handler = web_server.MAARequestHandler.__new__(web_server.MAARequestHandler)
        handler._send_json = Mock()

        def poll(cursor):
            handler.path = f"/api/logs?since={cursor}"
            handler.do_GET()
            return handler._send_json.call_args.args[0]

        with patch.object(web_server, "state", state):
            for index in range(300):
                state.add_log(str(index))
            initial = poll(0)
            self.assertEqual(initial["next_cursor"], 300)

            state.add_log("300")
            updated = poll(initial["next_cursor"])
            self.assertEqual(updated["next_cursor"], 301)
            self.assertEqual([log["text"] for log in updated["logs"]], ["300"])
            self.assertEqual(poll(301)["logs"], [])

            for index in range(301, 650):
                state.add_log(str(index))
            for cursor in (301, 900):
                caught_up = poll(cursor)
                self.assertEqual(caught_up["next_cursor"], 650)
                self.assertEqual([log["text"] for log in caught_up["logs"]],
                                 [str(index) for index in range(350, 650)])
