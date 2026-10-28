"""Offline regression tests: completion, payload budgets and persistence boundary."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import llm
from app.agents import orchestrator as agent
from app.services import chat_service


def finish(reason="stop", reasoning=""):
    return {"type": "completion", "finish_reason": reason, "reasoning_content": reasoning}


def call(cid="one", args='{"x":1}'):
    return {"id": cid, "type": "function", "function": {"name": "test", "arguments": args}}


def tool_round(calls):
    return [{"type": "tool_calls", "tool_calls": calls}, finish("tool_calls", "private reasoning")]


class GenerationTests(unittest.TestCase):
    def test_guide_guidance_is_once_after_complete_tools_and_preserves_system(self):
        snapshots = []
        guide_call = {"id": "guide", "function": {"name": "search_wiki", "arguments": '{"query":"team","entity_name":"1"}'}}
        batches = [tool_round([guide_call]), tool_round([call()]),
                   [{"type": "delta", "content": "answer"}, finish()]]
        def stream(messages, **kwargs):
            snapshots.append(list(messages))
            return iter(batches[len(snapshots)-1])
        guide = json.dumps({"game_version": "3.7", "results": [{"kind": "team_guide", "source_refs": []}]})
        with patch.object(agent, "stream_completion", side_effect=stream), patch.object(agent, "execute_tool", side_effect=[guide, '{}']):
            list(agent.run_agent([{"role": "system", "content": "unchanged"}], tools=[{}]))
        self.assertEqual(snapshots[1][-1], {"role": "user", "content": agent.GUIDE_RESPONSE_RULES})
        self.assertEqual(snapshots[1][-2]["role"], "tool")
        self.assertEqual(sum(m.get("content") == agent.GUIDE_RESPONSE_RULES for m in snapshots[-1]), 1)
        self.assertEqual(snapshots[-1][0], {"role": "system", "content": "unchanged"})

    def run_events(self, batches, **kwargs):
        with patch.object(agent, "stream_completion", side_effect=[iter(b) for b in batches]):
            return list(agent.run_agent([{"role": "user", "content": "question"}], tools=[{}], **kwargs))

    def test_stop_required(self):
        for reason in ("length", None, "content_filter"):
            with self.subTest(reason=reason), self.assertRaises(agent.IncompleteCompletionError):
                self.run_events([[{"type": "delta", "content": "partial"}, finish(reason)]], max_tokens=4096)

    def test_empty_budget_retry_once_and_account_usage(self):
        batches = [[{"type": "usage", "usage": {"completion_tokens": 4096}}, finish("length")],
                   [{"type": "delta", "content": "complete"}, finish()]]
        with patch.object(agent, "stream_completion", side_effect=[iter(b) for b in batches]) as stream:
            events = list(agent.run_agent([], max_tokens=4096))
        self.assertEqual(stream.call_args.kwargs["max_tokens"], 8192)
        self.assertEqual(sum(e["type"] == "retry" for e in events), 1)
        self.assertTrue(any(e["type"] == "usage" for e in events))
        with self.assertRaises(agent.IncompleteCompletionError):
            self.run_events([[finish("length")], [finish("length")]], max_tokens=2048)

    def test_stop_without_text_and_missing_terminal_rejected(self):
        for batch in ([finish()], [{"type": "delta", "content": "partial"}]):
            with self.assertRaises(agent.IncompleteCompletionError):
                self.run_events([batch], max_tokens=8192)

    def test_repeated_tool_dedup_and_reasoning_private(self):
        snapshots = []
        batches = [tool_round([call(), call("two", '{ "x": 1 }')]),
                   [{"type": "delta", "content": "answer"}, finish()]]
        def stream(messages, **kwargs):
            snapshots.append(list(messages))
            return iter(batches[len(snapshots) - 1])
        with patch.object(agent, "stream_completion", side_effect=stream), patch.object(agent, "execute_tool", return_value='{"fact":1}') as execute:
            events = list(agent.run_agent([], tools=[{}]))
        execute.assert_called_once()
        self.assertEqual(snapshots[1][0]["reasoning_content"], "private reasoning")
        self.assertTrue(json.loads(snapshots[1][2]["content"])["already_returned"])
        self.assertNotIn("private reasoning", json.dumps(events))

    def test_large_tool_returns_valid_error_not_cut_json(self):
        snapshots = []
        batches = [tool_round([call()]), [{"type": "delta", "content": "need narrow query"}, finish()]]
        def stream(messages, **kwargs):
            snapshots.append(list(messages))
            return iter(batches[len(snapshots) - 1])
        with patch.object(agent, "stream_completion", side_effect=stream), patch.object(agent, "execute_tool", return_value=json.dumps({"text": "x" * 15000})):
            list(agent.run_agent([], tools=[{}]))
        self.assertEqual(json.loads(snapshots[1][1]["content"])["error"], "tool_payload_budget")

    def test_wrap_up_disables_tools_after_bounded_rounds(self):
        snapshots = []
        batches = [tool_round([call(str(i))]) for i in range(agent.MAX_TOOL_ROUNDS)]
        batches.append([{"type": "delta", "content": "final"}, finish()])
        def stream(messages, **kwargs):
            snapshots.append((list(messages), kwargs))
            return iter(batches[len(snapshots) - 1])
        with patch.object(agent, "stream_completion", side_effect=stream), patch.object(agent, "execute_tool", return_value='{}'):
            list(agent.run_agent([], tools=[{}]))
        self.assertIsNone(snapshots[-1][1]["tools"])
        self.assertEqual(snapshots[-1][1]["max_tokens"], 8192)
        self.assertEqual(snapshots[-1][0][-1]["content"], agent.WRAP_UP_PROMPT)

    def test_truncation_not_persisted_or_done(self):
        def incomplete(*args, **kwargs):
            yield {"type": "delta", "content": "partial"}
            raise agent.IncompleteCompletionError("length")
        with patch.object(chat_service, "run_agent", side_effect=incomplete), \
             patch.object(chat_service, "build_system_prompt", return_value="system"), \
             patch.object(chat_service.context_service, "prepare_context", return_value=[]), \
             patch.object(chat_service.session_service, "list_messages", return_value=[]), \
             patch.object(chat_service.session_service, "append_message") as append:
            events = chat_service.stream_reply(NS(rollback=lambda: None), NS(id="session", owner_id=1), NS(), "question")
            self.assertEqual(json.loads(next(events))["type"], "delta")
            with self.assertRaises(agent.IncompleteCompletionError):
                list(events)
            self.assertEqual(append.call_count, 1)
            self.assertEqual(append.call_args.args[2], "user")

    def test_adapter_retains_finish_and_reasoning(self):
        chunks = [NS(usage=None, choices=[NS(finish_reason=None, delta=NS(content=None, reasoning_content="hidden", tool_calls=None))]),
                  NS(usage=None, choices=[NS(finish_reason="stop", delta=NS(content="answer", tool_calls=None))])]
        client = NS(chat=NS(completions=NS(create=lambda **kwargs: iter(chunks))))
        with patch.object(llm, "_get_client", return_value=client):
            events = list(llm.stream_completion([]))
        self.assertEqual(events[-1], finish("stop", "hidden"))
        self.assertEqual([e["content"] for e in events if e["type"] == "delta"], ["answer"])

    def test_budget_fallback_replaces_partial_and_persists_only_notice(self):
        for text in ("", "unfinished advice"):
            def incomplete(*args, **kwargs):
                if text:
                    yield {"type": "delta", "content": text}
                raise agent.IncompleteCompletionError("budget exhausted", reason="length")
            with self.subTest(text=text), patch.object(chat_service, "run_agent", side_effect=incomplete), \
                 patch.object(chat_service, "build_system_prompt", return_value="system"), \
                 patch.object(chat_service.context_service, "prepare_context", return_value=[]), \
                 patch.object(chat_service.session_service, "list_messages", return_value=[]), \
                 patch.object(chat_service.session_service, "append_message", return_value=NS(id=7)) as append:
                events = [json.loads(e) for e in chat_service.stream_reply(NS(rollback=lambda: None), NS(id="session", owner_id=1), NS(), "question")]
                self.assertEqual(append.call_count, 2)
                self.assertEqual(append.call_args.args[3], chat_service.BUDGET_FALLBACK)
                self.assertEqual(events[-1]["status"], "fallback")
                self.assertEqual(events[-2], {"type": "replace", "content": chat_service.BUDGET_FALLBACK})

    def test_reasoning_effort_is_forwarded(self):
        from unittest.mock import Mock
        create = Mock(return_value=iter([]))
        client = NS(chat=NS(completions=NS(create=create)))
        with patch.object(llm, "_get_client", return_value=client):
            list(llm.stream_completion([], reasoning_effort="low", max_tokens=4096))
        self.assertEqual(create.call_args.kwargs["reasoning_effort"], "low")

    def test_title_uses_small_non_thinking_budget(self):
        from unittest.mock import Mock
        create = Mock(return_value=NS(choices=[NS(finish_reason="stop", message=NS(content="title"))]))
        with patch.object(llm, "_get_client", return_value=NS(chat=NS(completions=NS(create=create)))):
            self.assertEqual(llm.chat([]), "title")
        self.assertEqual(create.call_args.kwargs["max_tokens"], 128)
        self.assertEqual(create.call_args.kwargs["extra_body"]["thinking"]["type"], "disabled")

    def test_turn_budget_prevents_another_call(self):
        from dataclasses import replace
        limited = replace(agent.settings, llm_turn_max_tokens=4096)
        batch = tool_round([call()])
        batch.insert(0, {"type": "usage", "usage": {"completion_tokens": 4096}})
        with patch.object(agent, "settings", limited), \
             patch.object(agent, "stream_completion", return_value=iter(batch)) as stream, \
             patch.object(agent, "execute_tool", return_value='{}'):
            with self.assertRaises(agent.IncompleteCompletionError) as caught:
                list(agent.run_agent([], tools=[{}], max_tokens=4096))
        self.assertEqual(caught.exception.reason, "turn_budget")
        stream.assert_called_once()

    def test_empty_stop_retry_does_not_raise_following_tool_budget(self):
        batches = [[finish()], tool_round([call()]), [{"type": "delta", "content": "complete"}, finish()]]
        with patch.object(agent, "stream_completion", side_effect=[iter(b) for b in batches]) as stream, \
             patch.object(agent, "execute_tool", return_value='{}'):
            list(agent.run_agent([], tools=[{}], max_tokens=4096))
        self.assertEqual([c.kwargs["max_tokens"] for c in stream.call_args_list], [4096, 8192, 4096])


if __name__ == "__main__":
    unittest.main()
