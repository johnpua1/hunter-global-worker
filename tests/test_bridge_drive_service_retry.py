"""Production regression: Apps Script JSON 'Service error: Drive' on saved US READ_CACHE."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"hunter-global"))
import runner


def response(payload):
    r=mock.Mock(status_code=200)
    r.json.return_value=payload
    return r


class BridgeDriveServiceRetryTests(unittest.TestCase):
    def drive(self):
        d=runner.Drive.__new__(runner.Drive)
        d.url="https://script.google.com/macros/s/test/exec"
        d.key="testkey"
        d.http=mock.Mock()
        return d

    def test_exact_service_error_read_retries_and_preserves_result(self):
        d=self.drive()
        first=d.http
        first.post.return_value=response({"ok":False,"error":"Service error: Drive"})
        second=mock.Mock()
        second.post.return_value=response({"ok":True,"file":{"revision":"2","size":100}})
        with mock.patch.object(runner.requests,"Session",return_value=second),\
             mock.patch.object(runner.time,"sleep") as sleep:
            result=d._call("file",path="US/CONTROL/READ_CACHE/piece.gz",_attempts=2)
        self.assertEqual(result["file"]["size"],100)
        first.close.assert_called_once()
        second.post.assert_called_once()
        sleep.assert_called_once()

    def test_persistent_exact_service_error_stops_after_bounded_retries(self):
        d=self.drive()
        d.http.post.return_value=response({"ok":False,"error":"Service error: Drive"})
        next_session=mock.Mock()
        next_session.post.return_value=response({"ok":False,"error":"Service error: Drive"})
        with mock.patch.object(runner.requests,"Session",return_value=next_session),\
             mock.patch.object(runner.time,"sleep"):
            with self.assertRaisesRegex(ValueError,"BRIDGE_READ_TRANSIENT_DRIVE_SERVICE"):
                d._call("file",path="US/file.gz",_attempts=2)
        self.assertEqual(next_session.post.call_count,1)

    def test_write_service_error_not_retried(self):
        d=self.drive()
        d.http.post.return_value=response({"ok":False,"error":"Service error: Drive"})
        with mock.patch.object(runner.time,"sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError,"BRIDGE_Service error: Drive"):
                d._call("put",path="US/control.json",_attempts=4)
        d.http.post.assert_called_once()
        sleep.assert_not_called()

    def test_deterministic_read_denial_not_retried(self):
        d=self.drive()
        d.http.post.return_value=response({"ok":False,"error":"BRIDGE_SCOPE_DENIED"})
        with mock.patch.object(runner.time,"sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError,"BRIDGE_SCOPE_DENIED"):
                d._call("file",path="HK/private",_attempts=4)
        d.http.post.assert_called_once()
        sleep.assert_not_called()


if __name__=="__main__":
    unittest.main()
