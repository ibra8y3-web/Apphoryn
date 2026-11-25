import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_system import AgentStore, _catalog_app_index, _poll_shared_catalog_once


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self):
        return "https://raw.githubusercontent.com/ibra8y3-web/Apphoryn/main/apps/web/catalog/index.json"

    def read(self, limit=-1):
        return self.payload[:limit]


class CatalogAutomationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tempdir.name) / "agents.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_system_state_is_persistent(self):
        self.store.set_system_state("agent_autopilot_enabled", True)
        reopened = AgentStore(Path(self.tempdir.name) / "agents.sqlite3")
        self.assertIs(reopened.get_system_state("agent_autopilot_enabled"), True)

    def test_catalog_index_tracks_release_and_artifact_facts(self):
        index = _catalog_app_index({
            "apps": [{
                "id": "app-one",
                "name": "App One",
                "repository_url": "https://github.com/example/app-one",
                "latest_version": "v2.0.0",
                "artifacts": [{
                    "filename": "app-one.apk",
                    "platform": "android",
                    "sha256": "A" * 64,
                }],
            }]
        })
        self.assertEqual(index["app-one"]["latest_version"], "v2.0.0")
        self.assertEqual(index["app-one"]["artifacts"][0]["sha256"], "a" * 64)

    def test_catalog_change_queues_one_read_only_niebex_review(self):
        old_manifest = {
            "apps": [{
                "id": "app-one",
                "name": "App One",
                "repository_url": "https://github.com/example/app-one",
                "latest_version": "v1.0.0",
                "release_url": "https://github.com/example/app-one/releases/tag/v1.0.0",
                "artifacts": [],
            }]
        }
        new_manifest = {
            "generated_at": "2026-11-25T00:00:00Z",
            "apps": [{
                "id": "app-one",
                "name": "App One",
                "repository_url": "https://github.com/example/app-one",
                "latest_version": "v2.0.0",
                "release_url": "https://github.com/example/app-one/releases/tag/v2.0.0",
                "artifacts": [{
                    "filename": "app-one.apk",
                    "platform": "android",
                    "sha256": "b" * 64,
                }],
            }]
        }
        old_index = _catalog_app_index(old_manifest)
        self.store.set_system_state("shared_catalog_snapshot", {
            "sha256": "previous-catalog-digest",
            "apps": old_index,
        })
        payload = json.dumps(new_manifest).encode("utf-8")
        fake = FakeResponse(payload)

        with patch("app.agent_system.STORE", self.store), \
             patch("app.agent_system.urlopen", return_value=fake), \
             patch.dict("os.environ", {"APPHORYN_AGENT_AUTOPILOT_ENABLED": "true"}, clear=False):
            _poll_shared_catalog_once()
            _poll_shared_catalog_once()

        runs = self.store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0]["allow_external_actions"])
        self.assertFalse(runs[0]["allow_code_changes"])
        self.assertIn("SHARED-CATALOG CHANGE REVIEW", runs[0]["goal"])
        health = self.store.get_system_state("shared_catalog_health")
        self.assertEqual(health["status"], "review_queued")
        self.assertEqual(health["updated_count"], 1)

    def test_persisted_disabled_switch_prevents_catalog_network_access(self):
        self.store.set_system_state("agent_autopilot_enabled", False)
        with patch("app.agent_system.STORE", self.store), \
             patch("app.agent_system.urlopen") as fetch:
            _poll_shared_catalog_once()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AutopilotCycleTests(unittest.TestCase):
    def setUp(self):
        import app.agent_system as agent_system
        self.agent_system = agent_system
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = agent_system.AgentStore(Path(self.tempdir.name) / "agents.sqlite3")
        self.patch = patch.object(agent_system, "STORE", self.store)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tempdir.cleanup()

    def test_default_is_continuous(self):
        phase = self.agent_system.autopilot_phase()
        self.assertTrue(phase["working"])
        self.assertEqual(phase["mode"], "continuous")

    def test_work_then_rest_cycle(self):
        self.store.set_system_state("agent_autopilot_schedule", {
            "work_minutes": 60, "rest_minutes": 30, "anchor": 1000.0,
        })
        phase = self.agent_system.autopilot_phase
        self.assertTrue(phase(1000.0 + 10 * 60)["working"])
        self.assertEqual(phase(1000.0 + 10 * 60)["seconds_remaining"], 50 * 60)
        self.assertFalse(phase(1000.0 + 70 * 60)["working"])
        self.assertEqual(phase(1000.0 + 70 * 60)["seconds_remaining"], 20 * 60)
        self.assertTrue(phase(1000.0 + 95 * 60)["working"])  # next cycle starts

    def test_saved_schedule_is_clamped_and_persistent(self):
        result = self.agent_system.set_autopilot_schedule(1, 99999)
        self.assertEqual(result["work_minutes"], 5)
        self.assertEqual(result["rest_minutes"], 1440)
        reopened = self.agent_system.AgentStore(Path(self.tempdir.name) / "agents.sqlite3")
        self.assertEqual(reopened.get_system_state("agent_autopilot_schedule")["work_minutes"], 5)

    def test_rest_window_skips_catalog_poll(self):
        self.store.set_system_state("agent_autopilot_enabled", True)
        self.store.set_system_state("agent_autopilot_schedule", {
            "work_minutes": 5, "rest_minutes": 1440, "anchor": __import__("time").time() - 600,
        })
        with patch.object(self.agent_system, "urlopen") as fake_open:
            self.agent_system._poll_shared_catalog_once()
        fake_open.assert_not_called()
