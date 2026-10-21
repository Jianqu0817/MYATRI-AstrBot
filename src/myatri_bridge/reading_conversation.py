"""Bridge-local adapter for the frozen myatri 0.2.7 conversation protocol.

respond mirrors vendor 0.2.7, with bounded model projections, read coverage and
lightweight audits. Keep retry/idempotency/budget/checkpoint semantics aligned
when updating that dependency. No changes to site-packages or the core checkout.
"""
from copy import deepcopy
import inspect
import json
import logging
import time

import jsonschema

from myatri.contracts import ModelRequest
from myatri.conversation.serialization import compact_json
from myatri.conversation.engine import Conversation, TOOLS
from myatri.providers.gateway import ProviderError, assistant_message, validate_protocol
from .tool_reading import (READING_RULES, FIELDS, cold_page, guard_reading_claim, read_audit,
                           read_page, recall_directory)
from .bubbles import prepare_bubbles

logger = logging.getLogger(__name__)


READING_TOOLS = deepcopy(TOOLS)
READING_TOOLS[0]["function"]["description"] = "搜索记忆目录；摘录不是已读原文，用read_record读取精确版本"
reader = READING_TOOLS[1]["function"]
reader["description"] = "分页读取精确ID@revision；按read_progress.first_unread_offset补缺页，末页不代表读完"
reader["parameters"]["properties"]["ref"]["maxLength"] = 256
reader["parameters"]["properties"]["field"] = {"type": "string", "enum": list(FIELDS)}


class ReadingConversation(Conversation):
    @classmethod
    def adapt(cls, conversation, chat_options=None):
        # Transfer the already constructed hooks, context, lock and shared
        # budgets without reinitializing runtime state or changing its class.
        adapted = cls.__new__(cls)
        adapted.__dict__.update(conversation.__dict__)
        options = chat_options or {}
        adapted.thinking = options.get("thinking", False)
        adapted.reasoning_effort = options.get("reasoning_effort", "high")
        adapted.thinking_max_tokens = options.get("thinking_max_tokens", 8192)
        adapted.thinking_timeout = options.get("thinking_timeout", 120)
        if (type(adapted.thinking) is not bool
                or not isinstance(adapted.reasoning_effort, str)
                or adapted.reasoning_effort not in {"low", "high", "max"}
                or type(adapted.thinking_max_tokens) is not int
                or not 1024 <= adapted.thinking_max_tokens <= 393216
                or type(adapted.thinking_timeout) not in {int, float}
                or not 0 < adapted.thinking_timeout <= 600):
            raise ValueError("invalid conversation thinking settings")
        if adapted.thinking:
            adapted.context.budget.output_reserve = max(
                adapted.context.budget.output_reserve, adapted.thinking_max_tokens)
        return adapted

    async def respond(self, text, *, message_id=None, metadata=None):
        started = time.perf_counter()
        async with self._lock:
            acquired = time.perf_counter()
            timings = {"lock_wait_seconds": acquired-started, "model_seconds": 0.,
                       "guard_seconds": 0., "tool_seconds": 0.}
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("message metadata must be a dict")
            if metadata and set(metadata) & {"role", "content", "message_id"}:
                raise ValueError("message metadata cannot override the conversation identity")
            raw = self.store.append_message("user", text, message_id=message_id, **(metadata or {}))
            try:
                answer = self.store.get_record("answer_" + raw.record_id)
            except KeyError:
                pass
            else:
                if self.on_answer:
                    self.on_answer(answer)
                return answer
            # Queue ingestion before API wait; user text remains safe if the provider fails.
            if self.on_message:
                self.on_message(raw)
            tools = [*READING_TOOLS, *[entry[0] for entry in self.tool_handlers.values()]] if self.enable_tools else []
            completed = sum(1 for r in self.store.list_records("raw_message")
                            if r.payload.get("role") == "assistant" and r.payload.get("reply_to"))
            checkpoint = (completed + 1) % self.context.trace_every_turns == 0
            timings["input_seconds"] = time.perf_counter()-acquired
            context_started = time.perf_counter()
            saved = self.protocol_store.get_protocol(raw.record_id) if self.protocol_store else None
            if saved and saved["status"] == "context_reset":
                raise ProviderError("turn belongs to a cleared context; use a new message ID")
            if saved and saved["status"] == "active":
                messages, snapshot, start_step = saved["messages"], saved["snapshot"], saved["steps"]
                turn_stats = deepcopy(saved.get("context_stats", {}))
                validate_protocol(messages)
            else:
                messages, snapshot = await self.context.abuild_context(text, tools=tools, persist_activation=checkpoint)
                turn_stats = deepcopy(self.context.explain_context().get("context_stats", {}))
                start_step = 0
            timings["context_seconds"] = time.perf_counter()-context_started
            preparation = self.context.explain_context().get("preparation_timings", {})
            rules = READING_RULES
            if "web_search" in self.tool_handlers:
                from .web_tools import WEB_SEARCH_RULES
                # The tool description covers "how"; this rule covers "when".
                rules = rules + "\n" + WEB_SEARCH_RULES
            if not any(m.get("role") == "system" and m.get("content") == rules for m in messages):
                messages.insert(0, {"role": "system", "content": rules})
            for step in range(start_step, self.max_steps):
                reply_policy = self.context.reply_policy
                output_tokens = self.context.budget.output_reserve
                if self.thinking:
                    output_tokens = self.thinking_max_tokens
                elif reply_policy.maximum:
                    output_tokens = min(output_tokens, reply_policy.max_tokens)
                request = ModelRequest(messages=messages, tools=tools, role="foreground", purpose="conversation",
                                       budget_tokens=self.context.budget.limit, max_tokens=output_tokens,
                                       thinking=self.thinking, reasoning_effort=self.reasoning_effort,
                                       timeout=self.thinking_timeout if self.thinking else 45,
                                       estimated_input_tokens=self.context.budget.estimate(messages, tools))
                self.context.budget.require(messages, tools)
                model_started = time.perf_counter()
                response = await self.gateway.generate(request)
                timings["model_seconds"] += time.perf_counter()-model_started
                self.context.budget.calibrate(messages, response.usage, tools)
                if self.protocol_store and checkpoint:
                    self.protocol_store.set_meta("context_token_anchor", self.context.budget.anchor)
                if not response.tool_calls:
                    if self.reply_guard is not None:
                        guard_started = time.perf_counter()
                        checked = await self.reply_guard(response.content, raw, messages)
                        timings["guard_seconds"] += time.perf_counter()-guard_started
                        response = response.model_copy(update={"content": checked})
                    guarded, reading_guard = guard_reading_claim(response.content, read_audit(messages))
                    body, reply_parts = prepare_bubbles(guarded, reply_policy)
                    response = response.model_copy(update={"content": body})
                    if not response.content:
                        raise ProviderError("empty reply after metadata removal; raw user message preserved")
                messages.append(assistant_message(response))
                if not response.tool_calls:
                    validate_protocol(messages)
                    answer = self.store.append_message("assistant", response.content,
                        message_id="answer_" + raw.record_id, reply_to=raw.ref,
                        provider=response.provider, model=response.model, context_snapshot=snapshot,
                        reading_guard=reading_guard, reply_parts=reply_parts,
                        **({"quality_review": self.reply_guard.last_check} if self.reply_guard is not None
                           and hasattr(self.reply_guard, "last_check") else {}))
                    timings["total_seconds"] = time.perf_counter()-started
                    timings["local_other_seconds"] = max(0., timings["total_seconds"] - sum(
                        value for key, value in timings.items() if key != "total_seconds"))
                    latency = {key: round(value, 4) for key, value in timings.items()}
                    latency["context_preparation"] = preparation
                    logger.info("MYATRI reply generation latency: %s", compact_json(latency))
                    if self.protocol_store:
                        protocol = {"status": "completed", "snapshot": snapshot,
                            "steps": step+1, "reply_ref": answer.ref, "sent_at": raw.created_at,
                            "replied_at": answer.created_at, "full_checkpoint": checkpoint,
                            "tool_audit": read_audit(messages), "reading_guard": reading_guard,
                            "context_stats": turn_stats, "latency": latency}
                        if self.thinking:
                            # Provider protocol data, never character memory. Needed
                            # when a later tools-enabled request replays this answer.
                            protocol["reasoning_content"] = response.provider_fields.get("reasoning_content", "")
                        if checkpoint:
                            protocol["messages"] = messages
                            protocol["context"] = self.context.explain_context()
                        self.protocol_store.save_protocol(raw.record_id, protocol)
                    if checkpoint:
                        self.store.flush_projection()
                    if self.on_answer:
                        self.on_answer(answer)
                    return answer
                tools_started = time.perf_counter()
                for call in response.tool_calls:
                    try:
                        name = call["function"]["name"]
                        definition = next(t["function"] for t in tools if t["function"]["name"] == name)
                        args = json.loads(call["function"]["arguments"])
                        jsonschema.validate(args, definition["parameters"])
                        if name == "recall":
                            bundle = self.recall.recall(args["query"], snapshot=snapshot, budget=1800)
                            result = recall_directory(bundle, self.store)
                        elif name in self.tool_handlers:
                            result = self.tool_handlers[name][1](**args)
                            # A blocking backend (e.g. an out-of-process search) must
                            # not stall the loop shared by every group, so async
                            # handlers are awaited and run in their own worker thread.
                            if inspect.isawaitable(result):
                                result = await result
                            if name == "read_group_message":
                                result = cold_page(result, args["message_id"], read_audit(messages))
                        else:
                            result = read_page(self.store, args["ref"], snapshot, read_audit(messages),
                                offset=args.get("offset", 0), limit=args.get("limit", 800), field=args.get("field"))
                        # Bounded result, without truncating JSON/protocol fields.
                        encoded = compact_json(result)
                        if len(encoded.encode("utf-8")) > 6000:
                            result = {"error": "record exceeds tool result budget", "ref": args.get("ref"), "suggestion": "use recall with narrower query"}
                    except (ValueError, KeyError, StopIteration, jsonschema.ValidationError) as exc:
                        result = {"error": type(exc).__name__, "message": "tool arguments or source invalid"}
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": compact_json(result)})
                timings["tool_seconds"] += time.perf_counter()-tools_started
                if self.protocol_store:
                    self.protocol_store.save_protocol(raw.record_id, {"status": "active", "snapshot": snapshot,
                        "steps": step+1, "messages": messages, "tool_audit": read_audit(messages),
                        "context_stats": turn_stats})
            raise ProviderError("tool step limit reached; raw user message preserved, no fictitious reply committed")
