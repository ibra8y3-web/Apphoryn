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





    def test_duplicate_active_goal_reuses_run_without_escalating_external_approval(self):
        first = self.store.enqueue("Check this exact upstream release.", priority=3, allow_external_actions=False)
        second = self.store.enqueue("  check this exact upstream release.  ", priority=9, allow_external_actions=True)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(second["allow_external_actions"])
        self.assertEqual(second["priority"], 3)

    def test_agent_activity_reports_real_latest_task_status(self):
        run = self.store.enqueue("Review the package metadata.", priority=5)
        task = self.store.create_task(run["id"], "research", "Inspect metadata", "Inspect the upstream release.")
        self.store.update_task(task, "running")
        activity = self.store.agent_activity()
        self.assertEqual(activity[0]["agent_id"], "research")
        self.assertEqual(activity[0]["task_count"], 1)
        self.assertEqual(activity[0]["latest_status"], "running")
        self.assertTrue(activity[0]["last_activity_at"])


    def test_code_change_permission_is_separate_from_external_action_approval(self):
        run = self.store.enqueue("Repair a broken API route safely.", allow_external_actions=True,
                                 allow_code_changes=False)
        self.assertTrue(run["allow_external_actions"])
        self.assertFalse(run["allow_code_changes"])

    def test_software_engineer_never_writes_without_code_change_approval(self):
        result = _run_tool("software_engineer", {
            "name": "apply_code_patch",
            "arguments": {
                "repository_url": "https://github.com/example/project",
                "files": [{"path": "app/example.py", "content": "VALUE = 1"}],
            },
        }, "Repair the code.", True, None, False)
        self.assertFalse(result["executed"])
        self.assertIn("code-change approval", result["reason"])

    def test_approved_code_change_still_requires_server_side_enablement(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"APPHORYN_CODE_WRITES_ENABLED": "false"}, clear=False):
            result = _run_tool("software_engineer", {
                "name": "apply_code_patch",
                "arguments": {
                    "repository_url": "https://github.com/example/project",
                    "files": [{"path": "app/example.py", "content": "VALUE = 1"}],
                },
            }, "Repair the code.", True, None, True)
        self.assertFalse(result["executed"])
        self.assertIn("server policy", result["reason"])

    def test_shared_memory_agent_tool_reads_and_saves_real_records(self):
        from unittest.mock import patch
        from app.agent_system import _run_tool
        with patch("app.agent_system.STORE", self.store):
            saved = _run_tool("writing", {
                "name": "shared_memory",
                "arguments": {"operation": "save", "scope": "agent", "content": {"style": "evidence first"}}
            }, "Write an Apphoryn summary.", False)
            read = _run_tool("supervisor", {
                "name": "shared_memory",
                "arguments": {"operation": "read", "limit": 3}
            }, "Review the task.", False)
        self.assertTrue(saved["saved"])
        self.assertEqual(read["memories"][0]["content"]["style"], "evidence first")

    def test_supervisor_quality_review_checks_actual_previous_agent_results(self):
        from app.agent_system import _run_tool
        results = [
            {"agent_id": "research", "result": {
                "verified_tool_result": {"executed": False}, "missing_evidence": ["official release checksum"]
            }},
            {"agent_id": "writing", "result": {"summary": "A grounded draft."}},
        ]
        audit = _run_tool("supervisor", {
            "name": "quality_review", "arguments": {}
        }, "Review outputs.", False, results)
        self.assertTrue(audit["executed"])
        self.assertEqual(audit["agents_reviewed"], 2)
        self.assertEqual(audit["agents_without_tool_evidence"], ["research"])
        self.assertEqual(audit["unresolved_evidence"], ["official release checksum"])


if __name__ == "__main__":
    unittest.main()
