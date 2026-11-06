import os
import sys
import tempfile
import unittest
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sigma.agent import (AgentRuntime, LocalToolExecutor, PolicyViolation, SecurePolicy, Task,
                         LocalProgrammerAgent, ProgramModeSession)


class TestSecureAgent(unittest.TestCase):
    def test_workdir_is_isolated_and_blocks_outside_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = SecurePolicy(workspace_root=tmp)
            outside = os.path.join(os.path.dirname(tmp), "outside.txt")
            self.assertFalse(policy.allow_write(outside))
            self.assertTrue(policy.allow_write(os.path.join(tmp, "src", "main.py")))

    def test_destructive_actions_need_explicit_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = SecurePolicy(workspace_root=tmp)
            runtime = AgentRuntime(policy, mode="programar")
            task = Task("delete generated artifacts", action="delete", path=os.path.join(tmp, "tmp", "a.py"))
            self.assertRaises(PolicyViolation, runtime.execute_task, task, approved=False)
            task.approved = True
            self.assertEqual(runtime.execute_task(task, approved=True)["status"], "approved")

    def test_program_mode_is_safe_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(SecurePolicy(workspace_root=tmp), mode="programar")
            self.assertEqual(runtime.mode, "programar")
            self.assertFalse(runtime.can_remote_control())
            self.assertFalse(runtime.can_access_network())

    def test_cowork_mode_is_human_supervised(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(SecurePolicy(workspace_root=tmp), mode="co-work")
            self.assertEqual(runtime.mode, "co-work")
            self.assertTrue(runtime.requires_human_approval_for("deploy"))
            self.assertTrue(runtime.requires_human_approval_for("git_push"))

    def test_programmer_agent_breaks_work_into_safe_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(SecurePolicy(workspace_root=tmp), mode="programar")
            agent = LocalProgrammerAgent(runtime)
            plan = agent.plan_task(Task("corrija a falha e rode os testes", action="test"))
            self.assertEqual(plan["risk"], "low")
            self.assertIn("steps", plan)
            self.assertTrue(plan["steps"])

            risky = agent.plan_task(Task("deletar artefatos antigos", action="delete", path=os.path.join(tmp, "build")))
            self.assertEqual(risky["risk"], "high")
            self.assertTrue(risky["requires_approval"])
            self.assertTrue(any(step["requires_approval"] for step in risky["steps"]))

    def test_programmer_agent_executes_only_local_file_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "input.txt")
            with open(source, "w", encoding="utf-8") as file:
                file.write("local")
            agent = LocalProgrammerAgent(AgentRuntime(SecurePolicy(tmp)))

            read_result = agent.run_task(Task("ler entrada", action="read", path=source))
            self.assertEqual(read_result["execution"]["content"], "local")
            write_task = Task("gravar saída", action="write", path=os.path.join(tmp, "output.txt"),
                              metadata={"content": "resultado"})
            with self.assertRaises(PolicyViolation):
                agent.run_task(write_task)
            with self.assertRaises(PolicyViolation):
                agent.run_task(write_task, approved=True)
            with self.assertRaises(PolicyViolation):
                agent.run_task(Task("rodar teste", action="test", command="pytest"))

    def test_programmer_agent_builds_single_file_context_and_unapproved_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "module.py"), "w", encoding="utf-8") as file:
                file.write("VALUE = 1\n")
            agent = LocalProgrammerAgent(AgentRuntime(SecurePolicy(tmp)))
            messages = agent.edit_messages("module.py", "adicione uma função")
            proposal = agent.parse_edit_proposal("module.py", "adicione uma função",
                json.dumps({"summary": "adicionar função", "content": "def answer():\n    return 42\n"}))

            self.assertEqual(proposal.action, "write")
            self.assertFalse(proposal.approved)
            self.assertEqual(proposal.metadata["content"], "def answer():\n    return 42\n")
            self.assertIn("VALUE = 1", messages[1]["content"])
            with self.assertRaises(PolicyViolation):
                agent.parse_edit_proposal("module.py", "edite", '{"summary": "bad", "content": 7}')

    def test_local_tools_search_workspace_and_generate_bounded_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "module.py")
            with open(source, "w", encoding="utf-8") as file:
                file.write("alpha\nneedle here\nomega\n")
            tools = LocalToolExecutor(AgentRuntime(SecurePolicy(tmp)), max_read_bytes=128,
                                      max_write_bytes=128, max_list_entries=10, max_search_files=10,
                                      max_search_results=2, max_diff_bytes=128)

            found = tools.search_text("needle")
            self.assertEqual(len(found["matches"]), 1)
            self.assertEqual(found["matches"][0]["path"], "module.py")
            diff = tools.diff_file("module.py", "alpha\nchanged\nomega\n")
            self.assertIn("-needle here", diff["diff"])
            self.assertIn("+changed", diff["diff"])

            short_output = LocalToolExecutor(AgentRuntime(SecurePolicy(tmp)), max_read_bytes=128,
                                             max_write_bytes=128, max_list_entries=10, max_search_files=10,
                                             max_search_results=1, max_diff_bytes=12)
            search = short_output.search_text("a")
            self.assertTrue(search["truncated"])
            limited_diff = short_output.diff_file("module.py", "replacement content")
            self.assertTrue(limited_diff["truncated"])
            self.assertLessEqual(len(limited_diff["diff"].encode("utf-8")), 12)

    def test_program_session_previews_write_before_approval_and_applies_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(SecurePolicy(workspace_root=tmp), mode="programar")
            session = ProgramModeSession(runtime)
            target = os.path.join(tmp, "result.py")
            task = Task("criar um arquivo de teste", action="write", path=target,
                        metadata={"content": "assert 2 + 2 == 4\n"})
            task_id = session.enqueue(task)
            self.assertEqual(session.status(task_id)["status"], "queued")
            session.advance(task_id)
            self.assertFalse(os.path.exists(target))
            preview = session.advance(task_id)
            self.assertIn("+assert 2 + 2 == 4", preview["result"]["diff"])
            self.assertEqual(preview["status"], "awaiting_approval")
            with self.assertRaises(PolicyViolation):
                session.advance(task_id)
            session.approve(task_id)
            result = session.advance(task_id)
            self.assertEqual(result["result"]["status"], "written")
            with open(target, encoding="utf-8") as file:
                self.assertEqual(file.read(), "assert 2 + 2 == 4\n")
            session.validate(task_id, passed=True)
            self.assertEqual(session.status(task_id)["status"], "completed")

    def test_program_session_rejects_proposal_changes_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Task("gravar arquivo", action="write", path=os.path.join(tmp, "x.txt"),
                        metadata={"content": "original"})
            session = ProgramModeSession(AgentRuntime(SecurePolicy(tmp)))
            task_id = session.enqueue(task)
            session.advance(task_id)
            session.advance(task_id)
            session.approve(task_id)
            task.metadata["content"] = "alterado depois da aprovação"
            with self.assertRaises(PolicyViolation):
                session.advance(task_id)
            self.assertFalse(os.path.exists(task.path))
            preview = session.advance(task_id)
            self.assertIn("+alterado depois da aprovação", preview["result"]["diff"])
            session.approve(task_id)
            session.advance(task_id)
            self.assertTrue(os.path.exists(task.path))

    def test_program_session_rejects_file_changes_after_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "existing.txt")
            with open(path, "w", encoding="utf-8") as file:
                file.write("base\n")
            task = Task("atualizar arquivo", action="write", path=path,
                        metadata={"content": "proposta\n"})
            session = ProgramModeSession(AgentRuntime(SecurePolicy(tmp)))
            task_id = session.enqueue(task)
            session.advance(task_id)
            preview = session.advance(task_id)
            self.assertIn("-base", preview["result"]["diff"])
            session.approve(task_id)
            with open(path, "w", encoding="utf-8") as file:
                file.write("alterado por outra pessoa\n")

            with self.assertRaises(PolicyViolation):
                session.advance(task_id)
            with open(path, encoding="utf-8") as file:
                self.assertEqual(file.read(), "alterado por outra pessoa\n")
            new_preview = session.advance(task_id)
            self.assertIn("-alterado por outra pessoa", new_preview["result"]["diff"])
            session.approve(task_id)
            session.advance(task_id)
            with open(path, encoding="utf-8") as file:
                self.assertEqual(file.read(), "proposta\n")

    def test_program_session_refuses_unimplemented_process_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ProgramModeSession(AgentRuntime(SecurePolicy(tmp)))
            task_id = session.enqueue(Task("run tests", action="test", command="pytest"))
            with self.assertRaises(PolicyViolation):
                session.advance(task_id, command="pytest")
            self.assertEqual(session.status(task_id)["status"], "queued")

    def test_program_session_can_search_locally_without_running_a_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "source.txt"), "w", encoding="utf-8") as file:
                file.write("find this text")
            session = ProgramModeSession(AgentRuntime(SecurePolicy(tmp)))
            task_id = session.enqueue(Task("buscar texto", action="search", metadata={"query": "this"}))
            session.advance(task_id)
            result = session.advance(task_id)
            self.assertEqual(result["result"]["matches"][0]["line"], 1)

    def test_allowlist_rejects_nonlocal_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = SecurePolicy(workspace_root=tmp)
            self.assertTrue(policy.allow_command("python -m unittest tests.test_agent_security"))
            self.assertFalse(policy.allow_command("curl https://example.com"))
            self.assertFalse(policy.allow_command("ssh user@host"))
            self.assertFalse(policy.allow_command('python -c "import os; os.remove(\'important.py\')"'))
            self.assertFalse(policy.allow_command("python -m http.server"))
            self.assertFalse(policy.allow_command("pytest; curl https://example.com"))

    def test_workspace_policy_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            link = os.path.join(tmp, "linked")
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not available")
            escaped = os.path.join(link, "secret.py")
            policy = SecurePolicy(workspace_root=tmp)
            self.assertFalse(policy.allow_read(escaped))
            self.assertFalse(policy.allow_write(escaped))

    def test_local_file_tools_are_bounded_and_write_requires_approval(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            source = os.path.join(tmp, "notes.txt")
            with open(source, "w", encoding="utf-8") as file:
                file.write("local context")
            runtime = AgentRuntime(SecurePolicy(workspace_root=tmp), mode="programar")
            tools = LocalToolExecutor(runtime, max_read_bytes=32)

            self.assertEqual(tools.read_file("notes.txt")["content"], "local context")
            self.assertEqual(tools.list_dir(".")["entries"], ["notes.txt"])
            with self.assertRaises(PolicyViolation):
                tools.read_file(os.path.join(outside, "secret.txt"))
            with self.assertRaises(PolicyViolation):
                tools.write_file("result.py", "print('safe')", approved=False)
            self.assertFalse(os.path.exists(os.path.join(tmp, "result.py")))

            result = tools.write_file("result.py", "print('safe')", approved=True)
            self.assertEqual(result["status"], "written")
            with open(os.path.join(tmp, "result.py"), encoding="utf-8") as file:
                self.assertEqual(file.read(), "print('safe')")

    def test_local_file_tool_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.txt", "b.txt", "large.txt"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as file:
                    file.write("0123456789" if name == "large.txt" else name)
            tools = LocalToolExecutor(AgentRuntime(SecurePolicy(tmp)), max_read_bytes=4,
                                      max_write_bytes=4, max_list_entries=2)

            listing = tools.list_dir()
            self.assertEqual(len(listing["entries"]), 2)
            self.assertTrue(listing["truncated"])
            with self.assertRaises(PolicyViolation):
                tools.read_file("large.txt")
            with self.assertRaises(PolicyViolation):
                tools.write_file("too-large.txt", "12345", approved=True)
            self.assertFalse(os.path.exists(os.path.join(tmp, "too-large.txt")))

    def test_session_summary_reports_pending_approvals_and_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(SecurePolicy(workspace_root=tmp), mode="programar")
            session = ProgramModeSession(runtime)
            task = Task("criar artefato", action="write", path=os.path.join(tmp, "build.txt"),
                        metadata={"content": "ok"})
            task_id = session.enqueue(task)

            summary = session.snapshot()
            self.assertEqual(summary["pending_approvals"], [])
            self.assertEqual(summary["tasks"][task_id]["status"], "queued")

            session.advance(task_id)
            session.advance(task_id)
            self.assertIn(task_id, session.snapshot()["pending_approvals"])
            session.approve(task_id)
            session.advance(task_id)
            summary = session.snapshot()

            self.assertEqual(summary["tasks"][task_id]["status"], "in_progress")
            self.assertEqual(summary["tasks"][task_id]["steps_done"], 3)
            self.assertEqual(summary["pending_approvals"], [])

    def test_rejected_task_is_not_pending_or_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ProgramModeSession(AgentRuntime(SecurePolicy(tmp)))
            task_id = session.enqueue(Task("gravar arquivo", action="write", path=os.path.join(tmp, "x.txt"),
                                           metadata={"content": "x"}))
            session.reject(task_id)
            self.assertEqual(session.snapshot()["pending_approvals"], [])
            with self.assertRaises(PolicyViolation):
                session.advance(task_id)


if __name__ == "__main__":
    unittest.main()
