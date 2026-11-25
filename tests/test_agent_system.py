import tempfile
import unittest
from pathlib import Path

from app.agent_system import AgentStore, _run_tool


class AgentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = AgentStore(Path(self.tempdir.name) / "agents.sqlite3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tasks_are_persisted_and_claimed_by_priority(self):
        low = self.store.enqueue("Review this source package.", priority=2)
        high = self.store.enqueue("Inspect this release metadata.", priority=9)
        claimed = self.store.claim_run("worker-test")
        self.assertEqual(claimed["id"], high["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(self.store.get_run(low["id"])["status"], "queued")

    def test_shared_memory_survives_store_reopen(self):
        self.store.save_memory("run", {"decision": "retain old artifacts"}, "run-1", "owner/repo")
        reopened = AgentStore(Path(self.tempdir.name) / "agents.sqlite3")
        memories = reopened.read_memories()
        self.assertEqual(memories[0]["content"]["decision"], "retain old artifacts")
        self.assertEqual(memories[0]["project_key"], "owner/repo")

    def test_schedule_creates_durable_schedule(self):
        schedule = self.store.create_schedule("Discover small open-source apps.", 3600, 5)
        self.assertTrue(schedule["enabled"])
        self.assertEqual(self.store.list_schedules()[0]["id"], schedule["id"])
        self.assertTrue(self.store.set_schedule_enabled(schedule["id"], False))
        self.assertFalse(self.store.list_schedules()[0]["enabled"])

    def test_import_tool_never_runs_without_explicit_external_approval(self):
        result = _run_tool("import", {
            "name": "import_repository",
            "arguments": {"repository_url": "https://github.com/owner/repository"}
        }, "Import this repository.", False)
        self.assertFalse(result["executed"])
        self.assertIn("approval", result["reason"].lower())


if __name__ == "__main__":
    unittest.main()
