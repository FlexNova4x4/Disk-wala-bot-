import io
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "cycle_controller.py"
SPEC = spec_from_file_location("cycle_controller", SCRIPT)
cycle_controller = module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(cycle_controller)


class CycleControllerTests(unittest.TestCase):
    def setUp(self):
        self.anchor = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)

    def test_parse_anchor_zulu(self):
        self.assertEqual(
            cycle_controller.parse_anchor("2026-11-16T00:00:00Z"), self.anchor
        )

    def test_parse_anchor_rejects_missing_timezone(self):
        with self.assertRaises(ValueError):
            cycle_controller.parse_anchor("2026-11-16T00:00:00")

    def test_online_at_anchor(self):
        state, position = cycle_controller.desired_state(self.anchor, self.anchor)
        self.assertEqual((state, position), ("online", 0.0))

    def test_online_through_5_hours_49_minutes_59_seconds(self):
        now = self.anchor.timestamp() + (5 * 60 + 50) * 60 - 1
        state, _ = cycle_controller.desired_state(
            self.anchor, datetime.fromtimestamp(now, timezone.utc)
        )
        self.assertEqual(state, "online")

    def test_offline_starts_at_5_hours_50_minutes(self):
        now = datetime.fromtimestamp(
            self.anchor.timestamp() + cycle_controller.ONLINE_SECONDS,
            timezone.utc,
        )
        state, _ = cycle_controller.desired_state(self.anchor, now)
        self.assertEqual(state, "offline")

    def test_offline_through_19_minutes_59_seconds(self):
        now = datetime.fromtimestamp(
            self.anchor.timestamp() + cycle_controller.ONLINE_SECONDS
            + cycle_controller.OFFLINE_SECONDS - 1,
            timezone.utc,
        )
        state, _ = cycle_controller.desired_state(self.anchor, now)
        self.assertEqual(state, "offline")

    def test_next_cycle_starts_after_6_hours_10_minutes(self):
        now = datetime.fromtimestamp(
            self.anchor.timestamp() + cycle_controller.PERIOD_SECONDS,
            timezone.utc,
        )
        state, position = cycle_controller.desired_state(self.anchor, now)
        self.assertEqual((state, position), ("online", 0.0))

    def test_before_anchor_is_offline(self):
        now = datetime.fromtimestamp(self.anchor.timestamp() - 1, timezone.utc)
        state, position = cycle_controller.desired_state(self.anchor, now)
        self.assertEqual((state, position), ("offline", 0.0))

    def test_rejects_non_https_webhook(self):
        with self.assertRaises(ValueError):
            cycle_controller.validate_webhook_url(
                "http://example.com/start", "HOST_START_WEBHOOK"
            )

    def test_rejects_webhook_with_embedded_credentials(self):
        with self.assertRaises(ValueError):
            cycle_controller.validate_webhook_url(
                "https://user:pass@example.com/start", "HOST_START_WEBHOOK"
            )

    def test_no_secrets_skips_without_network(self):
        env = {
            "CYCLE_ANCHOR_UTC": "",
            "HOST_START_WEBHOOK": "",
            "HOST_STOP_WEBHOOK": "",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            cycle_controller, "urlopen"
        ) as mocked_urlopen:
            result = cycle_controller.main()
        self.assertEqual(result, 0)
        mocked_urlopen.assert_not_called()

    def test_partial_secrets_fail_configuration(self):
        env = {
            "CYCLE_ANCHOR_UTC": "2026-11-16T00:00:00Z",
            "HOST_START_WEBHOOK": "https://example.com/start",
            "HOST_STOP_WEBHOOK": "",
        }
        with patch.dict(os.environ, env, clear=False):
            result = cycle_controller.main()
        self.assertEqual(result, 2)

    def test_webhook_posts_expected_json(self):
        class FakeResponse:
            status = 204
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with patch.object(cycle_controller, "urlopen", return_value=FakeResponse()) as mocked:
            status = cycle_controller.post_state("https://example.com/start", "online")
        self.assertEqual(status, 204)
        request = mocked.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data.decode()), {"desired_state": "online"})


if __name__ == "__main__":
    unittest.main()
