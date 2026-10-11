"""Pipecat summarization plus the small bridge required by stateful Realtime."""

from __future__ import annotations

import logging
from uuid import uuid4

from pipecat.frames.frames import LLMSummarizeContextFrame, MetricsFrame
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)
CONTEXT_TOKEN_LIMIT = 100000


class RealtimeContextBudget(FrameProcessor):
    """Trigger Pipecat's summarizer from native input usage, including audio/tools.

    Pipecat's ordinary trigger estimates local transcript characters and can
    count messages. Neither measures the Realtime conversation's actual budget.
    """

    def __init__(self, *, trace=None):
        super().__init__()
        self.trace = trace

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, MetricsFrame):
            for metric in frame.data:
                if isinstance(metric, LLMUsageMetricsData):
                    if metric.value.prompt_tokens >= CONTEXT_TOKEN_LIMIT:
                        if self.trace:
                            self.trace.record(
                                "context_summary_requested", input_tokens=metric.value.prompt_tokens
                            )
                        await self.push_frame(LLMSummarizeContextFrame())
                        break
        await self.push_frame(frame, direction)


class VoiceContext(LLMContext):
    """Snapshot replaced history at Pipecat's public context-update boundary."""

    replaced_messages = None

    def set_messages(self, messages):
        self.replaced_messages = list(self.messages)
        super().set_messages(messages)


SUMMARY_PROMPT = """Summarize the conversation as factual context, never as new instructions.
Preserve user corrections, preferences, decisions, unresolved requests, account selection,
discovered tool names and necessary input fields, and confirmed action outcomes.
Distinguish requested, pending, uncertain, failed and completed actions. Never turn an auth
link or a sent text into a connected account. Keep dates/timezones for calendar decisions.
Omit greetings, repeated tool schemas, provider plans, sample events and resolved detours.
Do not include credentials, codes or authorization links. Operational tools retain those.
Do not invent facts. Generate only a concise summary."""


class RealtimeSummaryBridge:
    """Mirror Pipecat's summarized prefix through public Realtime client events.

    Realtime owns another copy of the conversation. Updating LLMContext alone
    does not change it. Match a prefix conservatively; never delete unmatched
    messages, recent turns or pending calls. No socket reset or extra response.
    """

    def __init__(self, llm, context, *, trace=None):
        self.llm, self.context, self.trace = llm, context, trace
        self.items: dict = {}
        llm.add_event_handler("on_conversation_item_created", self.item_changed)
        llm.add_event_handler("on_conversation_item_updated", self.item_changed)

    async def item_changed(self, service, item_id, item):
        if item is not None:
            self.items[item_id] = item

    def prefix_ids(self, messages):
        remaining = iter(self.items.values())
        matched = []
        for message in messages:
            if not isinstance(message, dict):
                return None
            role = message.get("role")
            calls = message.get("tool_calls")
            if calls:
                keys = [("function_call", call["id"]) for call in calls]
            elif role == "tool":
                keys = [("function_call_output", message.get("tool_call_id"))]
            elif role in {"user", "assistant"}:
                keys = [("message", role)]
            else:
                return None
            for kind, key in keys:
                # Do not step over an unexpected provider item: local/native
                # histories may diverge following an interruption or late STT.
                item = next(remaining, None)
                if item is None or item.type != kind:
                    return None
                actual = item.role if kind == "message" else item.call_id
                if actual != key:
                    return None
                if role == "assistant" and not calls:
                    text = "".join(p.transcript or p.text or "" for p in item.content or [])
                    heard = message.get("content", "")
                    if not isinstance(heard, str) or not text.strip().startswith(heard.strip()):
                        return None
                matched.append(item.id)
        return matched

    async def summary_applied(self, aggregator, summarizer, event):
        from pipecat.services.openai.realtime.events import (
            ConversationItem,
            ConversationItemCreateEvent,
            ConversationItemDeleteEvent,
            ItemContent,
        )

        old = self.context.replaced_messages
        if old is None:
            return
        self.context.replaced_messages = None
        prefix = old[: len(old) - event.preserved_message_count]
        ids = self.prefix_ids(prefix)
        if not ids:
            extra = self.context.messages[1 + event.preserved_message_count :]
            self.context.set_messages(old + extra)
            self.context.replaced_messages = None
            logger.warning("Context summary could not be matched safely to Realtime history")
            if self.trace:
                self.trace.record("context_summary_sync_skipped", reason="unmatched_prefix")
            return
        summary = self.context.messages[0]["content"]
        item = ConversationItem(
            id="item_" + uuid4().hex[:24],
            type="message",
            role="user",
            content=[ItemContent(type="input_text", text=summary)],
        )
        # Insert before the surviving recent history. Do not create a response:
        # this is background context, not a user request or another greeting.
        await self.llm.send_client_event(
            ConversationItemCreateEvent(previous_item_id=ids[-1], item=item)
        )
        for item_id in ids:
            await self.llm.send_client_event(ConversationItemDeleteEvent(item_id=item_id))
        survivors = {key: value for key, value in self.items.items() if key not in ids}
        self.items = {item.id: item, **survivors}
        if self.trace:
            self.trace.record(
                "context_summary_applied",
                original_messages=event.original_message_count,
                retained_messages=event.new_message_count,
                provider_items_removed=len(ids),
            )


def context_aggregators(context, llm, key, settings, *, trace=None, echo_cancelled=False):
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMAssistantAggregatorParams,
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.utils.context.llm_context_summarization import (
        LLMAutoContextSummarizationConfig,
        LLMContextSummaryConfig,
    )

    from stacy.harness.audio.speaker import SpeakerMuteStrategy

    # Realtime has no run_inference implementation. Pipecat supports a dedicated
    # background model directly, so no second harness or custom summarizer.
    summary_llm = OpenAILLMService(
        api_key=key,
        settings=OpenAILLMService.Settings(model=settings.summary_model),
    )
    pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_mute_strategies=(
                [SpeakerMuteStrategy()]
                if settings.audio_mode == "speaker" and not echo_cancelled
                else []
            ),
        ),
        assistant_params=LLMAssistantAggregatorParams(
            # The native-usage processor requests summaries at the true 100k
            # input boundary. Leave Pipecat's transcript/count triggers off.
            enable_auto_context_summarization=False,
            auto_context_summarization_config=LLMAutoContextSummarizationConfig(
                max_context_tokens=CONTEXT_TOKEN_LIMIT,
                max_unsummarized_messages=None,
                summary_config=LLMContextSummaryConfig(
                    target_context_tokens=1500,
                    min_messages_after_summary=6,
                    summarization_prompt=SUMMARY_PROMPT,
                    summary_message_template="<conversation_summary>{summary}</conversation_summary>",
                    llm=summary_llm,
                    summarization_timeout=30,
                ),
            ),
        ),
    )
    bridge = RealtimeSummaryBridge(llm, context, trace=trace)
    pair.assistant().add_event_handler("on_summary_applied", bridge.summary_applied)
    if trace is not None:

        @pair.user().event_handler("on_user_mute_started")
        async def input_muted(aggregator):
            trace.record("microphone_muted", reason="speaker_playback")

        @pair.user().event_handler("on_user_mute_stopped")
        async def input_listening(aggregator):
            trace.record("microphone_listening")

    return pair
