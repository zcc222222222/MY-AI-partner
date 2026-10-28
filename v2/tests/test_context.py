"""Session memory and full-call input boundaries; all databases are temporary."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import create_engine, event, select, func, text
from sqlalchemy.orm import sessionmaker
from app import models
from app.migrations import migrate, HEAD
from app.agents.context_budget import input_cost, message_cost, recent_window
from app.agents.roleplay import build_context
from app.agents import orchestrator
from app.services import context_service as ctx, session_service, chat_service


class WindowTests(unittest.TestCase):
    def test_preserves_whole_turns_and_latest_user(self):
        history = [NS(id=i, role="user" if i % 2 else "assistant", content="问答" * 80) for i in range(1, 10)]
        result = recent_window(history, 1700)
        self.assertEqual(result[0].role, "user")
        self.assertEqual(result[-1].id, 9)
        self.assertEqual([m.id for m in result], list(range(result[0].id, 10)))
        self.assertLessEqual(input_cost(build_context(result)), 1702)
        self.assertEqual(build_context(history, budget=1700), build_context(result))

    def test_current_user_is_never_silently_truncated(self):
        with self.assertRaises(ValueError):
            recent_window([NS(id=1, role="user", content="🚀" * 500)], 100)

    def test_per_call_guard_blocks_before_network(self):
        config = replace(orchestrator.settings, llm_context_window_tokens=33000)
        with patch.object(orchestrator, "settings", config), patch.object(orchestrator, "stream_completion") as network:
            with self.assertRaises(orchestrator.IncompleteCompletionError) as error:
                list(orchestrator.run_agent([{"role": "user", "content": "汉" * 12000}]))
        self.assertEqual(error.exception.reason, "context_budget")
        network.assert_not_called()

    def test_reasoning_and_tool_schemas_count_towards_input(self):
        message = {"role": "assistant", "content": None, "tool_calls": [], "reasoning_content": "隐" * 100}
        self.assertGreater(input_cost([message], [{"description": "长" * 100}]), 600)

    def test_tool_result_over_input_budget_is_valid_error(self):
        snapshots = []
        batches = [[{"type": "tool_calls", "tool_calls": [{"id": "x", "function": {"name": "test", "arguments": "{}"}}]},
                    {"type": "completion", "finish_reason": "tool_calls", "reasoning_content": ""}],
                   [{"type": "delta", "content": "请缩小查询"}, {"type": "completion", "finish_reason": "stop"}]]
        def stream(messages, **kwargs):
            snapshots.append(list(messages))
            return iter(batches[len(snapshots) - 1])
        config = replace(orchestrator.settings, llm_context_window_tokens=33000)
        with patch.object(orchestrator, "settings", config), patch.object(orchestrator, "stream_completion", side_effect=stream), \
             patch.object(orchestrator, "execute_tool", return_value=json.dumps({"text": "长" * 10000}, ensure_ascii=False)):
            list(orchestrator.run_agent([], tools=[{}]))
        self.assertEqual(json.loads(snapshots[1][-1]["content"])["error"], "tool_payload_budget")


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + (Path(self.tmp.name) / "app.db").as_posix())
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        migrate(self.engine)
        self.db = sessionmaker(self.engine)()
        self.db.add_all([models.User(id=i, username=f"u{i}", display_name=f"u{i}", password_hash="unused") for i in (1, 2)])
        self.db.add(models.Character(id=1, slug="test", name="test", display_name="test", system_prompt="stable"))
        self.db.commit()
        self.db.add_all([models.ChatSession(id=sid, character_id=1, owner_id=owner) for sid, owner in (("a", 1), ("b", 2), ("other", 1))])
        self.db.commit()
        self.config = replace(ctx.settings, llm_history_budget=7000, llm_summary_budget=2000, llm_summary_input_budget=10000)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def history(self, count=24, sid="a"):
        for i in range(count):
            self.db.add(models.Message(session_id=sid, role="user" if i % 2 == 0 else "assistant",
                content=(f"第{i}条：角色和武器问题。" if i % 2 == 0 else "以前的回答不是新的事实来源。") * 15))
        self.db.add(models.Message(session_id=sid, role="user", content="更正：现在讨论女漂泊者·湮灭，版本3.7，我没有她的专武。"))
        self.db.commit()
        return [NS(id=m.id, role=m.role, content=m.content) for m in session_service.list_messages(self.db, sid, owner_id=1)]

    def prepare(self, history, sid="a", owner=1, authorize=None):
        with patch.object(ctx, "settings", self.config):
            return ctx.prepare_context(self.db, history, session_id=sid, owner_id=owner,
                system_prompt="stable", tools=[], authorize=authorize)

    def test_short_conversation_has_no_summary_call_or_cache(self):
        history = self.history(count=2)
        with patch.object(ctx, "select_memory") as model:
            self.assertEqual(self.prepare(history), build_context(history))
        model.assert_not_called()
        self.assertIsNone(self.db.get(models.ContextSummary, "a"))

    def test_bounded_summary_originals_untouched_and_reusable(self):
        history = self.history()
        def choose(candidates):
            return [candidates[0]["message_id"], candidates[-1]["message_id"]]
        count = len(history)
        with patch.object(ctx, "select_memory", side_effect=choose) as model:
            messages = self.prepare(history)
            second = self.prepare(history)
        model.assert_called_once()
        self.assertEqual(messages, second)
        self.assertLessEqual(input_cost(messages), self.config.llm_history_budget)
        self.assertEqual(messages[-1]["content"], history[-1].content)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(models.Message)), count)
        payload = json.loads(self.db.get(models.ContextSummary, "a").payload)
        self.assertTrue(all(type(i) is int for i in payload))
        summary = json.loads(messages[0]["content"][len(ctx.NOTICE):])
        original = {m.id: m.content for m in history}
        self.assertTrue(all(item["user_text"] == original[item["message_id"]] for item in summary))
        self.assertNotIn("以前的回答", messages[0]["content"])

    def test_selector_failure_falls_back_once_and_keeps_current_question(self):
        history = self.history()
        with patch.object(ctx, "select_memory", side_effect=TimeoutError) as model:
            first = self.prepare(history)
            second = self.prepare(history)
        model.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(first[-1]["content"], history[-1].content)
        self.assertLessEqual(input_cost(first), self.config.llm_history_budget)

    def test_cache_ids_revalidated_and_never_cross_sessions(self):
        history = self.history()
        self.db.add(models.Message(id=999, session_id="b", role="user", content="OTHER-ACCOUNT-SECRET"))
        self.db.add(models.ContextSummary(session_id="a", owner_id=1, covered_through=20,
            payload="[999,1]", format_version=ctx.FORMAT))
        self.db.commit()
        with patch.object(ctx, "select_memory", side_effect=lambda c: [i["message_id"] for i in c][-2:]):
            messages = self.prepare(history)
            other = self.prepare([NS(id=1000, role="user", content="other session")], sid="other")
        self.assertNotIn("OTHER-ACCOUNT-SECRET", json.dumps(messages))
        self.assertEqual(other, [{"role": "user", "content": "other session"}])

    def test_wrong_owner_cannot_write_and_deleted_session_not_recreated(self):
        with self.assertRaises(PermissionError):
            ctx._save(self.db, "a", 2, 10, [1], None)
        self.assertTrue(session_service.delete_session(self.db, "a", owner_id=1))
        with self.assertRaises(PermissionError):
            ctx._save(self.db, "a", 1, 10, [1], None)

    def test_slower_summary_cannot_move_watermark_back(self):
        ctx._save(self.db, "a", 1, 20, [19], None)
        ctx._save(self.db, "a", 1, 10, [9], None)
        cached = self.db.get(models.ContextSummary, "a")
        self.assertEqual(cached.covered_through, 20)
        self.assertEqual(json.loads(cached.payload), [19])

    def test_revoked_authorization_during_selection_prevents_save(self):
        history = self.history()
        authorized = Mock(side_effect=[None, PermissionError("revoked")])
        with patch.object(ctx, "select_memory", side_effect=lambda c: [c[0]["message_id"]]):
            with self.assertRaises(PermissionError):
                self.prepare(history, authorize=authorized)
        self.assertIsNone(self.db.get(models.ContextSummary, "a"))

    def test_delete_session_removes_summary_and_keeps_other_session(self):
        ctx._save(self.db, "a", 1, 10, [1], None)
        session_service.delete_session(self.db, "a", owner_id=1)
        self.assertIsNone(self.db.get(models.ContextSummary, "a"))
        self.assertIsNotNone(self.db.get(models.ChatSession, "other"))

    def test_migration_from_previous_schema_preserves_messages_and_backs_up(self):
        history = self.history(count=2)
        migrate(self.engine, "0003_email")
        result = migrate(self.engine)
        self.assertEqual(result["revision"], HEAD)
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual(len(session_service.list_messages(self.db, "a", owner_id=1)), len(history))
        self.assertFalse(migrate(self.engine)["changed"])

    def test_chat_service_keeps_system_and_persists_complete_answer(self):
        history = self.history()
        captured = []
        def agent(messages, **kwargs):
            captured.extend(messages)
            yield {"type": "delta", "content": "完整回答"}
        with patch.object(ctx, "settings", self.config), patch.object(ctx, "select_memory", side_effect=TimeoutError), \
             patch.object(chat_service, "run_agent", side_effect=agent):
            result = [json.loads(e) for e in chat_service.stream_reply(self.db,
                self.db.get(models.ChatSession, "a"), self.db.get(models.Character, 1), "继续比较这个角色")]
        from app.agents.roleplay import MARKDOWN_STYLE
        self.assertEqual(captured[0], {"role": "system", "content": "stable" + MARKDOWN_STYLE})
        self.assertEqual(sum(m["role"] == "system" for m in captured), 1)
        self.assertEqual(captured[-1]["content"], "继续比较这个角色")
        self.assertEqual(result[-1]["type"], "done")
        self.assertEqual(session_service.list_messages(self.db, "a", owner_id=1)[-1].content, "完整回答")


class SelectorTests(unittest.TestCase):
    def test_invalid_ids_and_incomplete_output_rejected(self):
        for output, reason in (({"message_ids": [999]}, "stop"), ({"message_ids": [True]}, "stop"),
                               ({"message_ids": [1, 1]}, "stop"), ({"message_ids": [1]}, "length")):
            create = Mock(return_value=NS(choices=[NS(finish_reason=reason, message=NS(content=json.dumps(output)))], usage=None))
            client = NS(with_options=Mock(return_value=NS(chat=NS(completions=NS(create=create)))))
            with self.subTest(output=output), patch.object(ctx.llm, "_get_client", return_value=client):
                with self.assertRaises(ValueError):
                    ctx.select_memory([{"message_id": 1, "user_text": "原始文本"}])

    def test_selector_only_returns_source_ids_with_bounded_request(self):
        create = Mock(return_value=NS(choices=[NS(finish_reason="stop", message=NS(content='{"message_ids":[1]}'))], usage=None))
        client = NS(with_options=Mock(return_value=NS(chat=NS(completions=NS(create=create)))))
        with patch.object(ctx.llm, "_get_client", return_value=client):
            self.assertEqual(ctx.select_memory([{"message_id": 1, "user_text": "我没有专武"}]), [1])
        self.assertEqual(create.call_args.kwargs["max_tokens"], 512)
        self.assertEqual(create.call_args.kwargs["extra_body"]["thinking"]["type"], "disabled")
        client.with_options.assert_called_once_with(timeout=15, max_retries=0)


if __name__ == "__main__":
    unittest.main()
