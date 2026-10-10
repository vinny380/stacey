import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("pipecat")
from pipecat.processors.aggregators.llm_context_summarizer import LLMContextSummarizer
from pipecat.services.openai.realtime.events import ConversationItem, ItemContent
from pipecat.utils.context.llm_context_summarization import (
    LLMAutoContextSummarizationConfig,
    LLMContextSummarizationUtil,
    LLMContextSummaryConfig,
)

from stacy.config import Settings
from stacy.context_management import RealtimeSummaryBridge, VoiceContext
from stacy.voice import build_voice_pipeline


class LLM:
    def __init__(self):
        self.sent = []

    def add_event_handler(self, *args):
        pass

    async def send_client_event(self, event):
        self.sent.append(event.model_dump(exclude_none=True))


def test_native_summary_preserves_recent_and_pending_calls_and_mirrors_provider():
    async def scenario():
        messages = [
            {"role": "user", "content": "Name is Vinny"},
            {"role": "assistant", "content": "Hi Vinny"},
            {"role": "user", "content": "Set a timer"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "pending", "function": {"name": "set_timer", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "pending", "content": "IN_PROGRESS"},
            {"role": "user", "content": "Actually five minutes"},
        ]
        context, llm = VoiceContext(messages), LLM()
        retained = list(messages[3:])
        bridge = RealtimeSummaryBridge(llm, context)
        for item in [
            ConversationItem(id="u1", type="message", role="user"),
            ConversationItem(
                id="a1",
                type="message",
                role="assistant",
                content=[ItemContent(type="output_audio", transcript="Hi Vinny")],
            ),
            ConversationItem(id="u2", type="message", role="user"),
            ConversationItem(id="f1", type="function_call", call_id="pending", name="set_timer"),
        ]:
            await bridge.item_changed(llm, item.id, item)
        selection = LLMContextSummarizationUtil.get_messages_to_summarize(context, 2)
        assert selection.last_summarized_index == 2
        summarizer = LLMContextSummarizer(
            context=context,
            config=LLMAutoContextSummarizationConfig(
                max_context_tokens=8000,
                summary_config=LLMContextSummaryConfig(
                    min_messages_after_summary=2,
                    summary_message_template="<conversation_summary>{summary}</conversation_summary>",
                ),
            ),
        )

        async def applied(s, e):
            await bridge.summary_applied(None, s, e)

        summarizer.add_event_handler("on_summary_applied", applied)
        await summarizer._apply_summary("User is Vinny; timer remains pending.", 2)
        await asyncio.sleep(0)
        assert context.messages[1:] == retained
        assert "pending" in context.messages[1]["tool_calls"][0]["id"]
        assert llm.sent[0]["previous_item_id"] == "u2"
        assert [e["item_id"] for e in llm.sent[1:]] == ["u1", "a1", "u2"]
        assert all(e["type"] != "response.create" for e in llm.sent)
        assert "f1" in bridge.items

    asyncio.run(scenario())


def test_diverged_history_is_not_deleted_or_replayed():
    async def scenario():
        context, llm = VoiceContext([{"role": "assistant", "content": "heard"}]), LLM()
        bridge = RealtimeSummaryBridge(llm, context)
        await bridge.item_changed(
            llm, "unexpected", ConversationItem(id="unexpected", type="message", role="user")
        )
        context.set_messages([{"role": "user", "content": "summary"}])
        await bridge.summary_applied(None, None, SimpleNamespace(preserved_message_count=0))
        assert not llm.sent

    asyncio.run(scenario())


def test_voice_uses_native_background_summarization_and_persistent_instructions(tmp_path):
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.processors.aggregators.llm_response_universal import LLMAssistantAggregator
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    incoming, outgoing = FrameProcessor(), FrameProcessor()
    pipeline = build_voice_pipeline(
        Settings(tmp_path, tmp_path / "memory"),
        "offline",
        ToolsSchema(standard_tools=[]),
        SimpleNamespace(input=lambda: incoming, output=lambda: outgoing),
    )
    assistant = next(p for p in pipeline.processors if isinstance(p, LLMAssistantAggregator))
    summarizer = assistant._summarizer
    assert summarizer._auto_trigger
    config = summarizer._auto_config
    assert config.max_context_tokens == 8000
    assert config.max_unsummarized_messages == 20
    assert config.summary_config.min_messages_after_summary == 6
    assert config.summary_config.llm.settings.model == "gpt-4.1-mini"
    llm = next(p for p in pipeline.processors if isinstance(p, OpenAIRealtimeLLMService))
    assert "You are Stacy" in llm.settings.system_instruction
    assert not assistant.context.messages
