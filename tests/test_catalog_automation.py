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
