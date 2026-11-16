"""Remote working-context compaction, preserving the complete session transcript.

The grouping and retained-tool-call rules follow skills-agent's WorkingContext.
Only the derived summary and its cursor change; original messages stay remote.
"""

from copy import deepcopy
import hashlib
import json


def message_text(message):
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content or [] if block.get("type") == "text")


def working_message(message):
    return deepcopy({key: value for key, value in message.items() if key != "_raw"})


def message_groups(messages):
    groups = []
    position = 0
    while position < len(messages):
        start = position
        message = messages[position]
        if message.get("role") == "tool":
            raise ValueError("Session contains an orphan tool result")
        calls = message.get("tool_calls", [])
        pending = {call["id"] for call in calls}
        if len(pending) != len(calls):
            raise ValueError("Session contains duplicate tool call IDs")
        position += 1
        while pending and position < len(messages):
            result = messages[position]
            identifier = result.get("tool_call_id")
            if result.get("role") != "tool" or identifier not in pending:
                break
            pending.remove(identifier)
            position += 1
        if pending:
            raise ValueError("Unfinished tool calls must be recovered before requesting the model")
        groups.append((start, position))
    return groups


def _digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _estimate(value):
    # Explicit fallback for providers without a counting API. It is not usage.
    return max(1, len(json.dumps(value, ensure_ascii=False).encode("utf-8")) // 3)


def prepare_context(session, model, system, tools, *, on_activity=None):
    messages = [working_message(message) for message in session.get("messages", [])]
    groups = message_groups(messages)
    state = deepcopy(session.get("context") or {})
    cursor = state.get("cursor", 0)
    if (type(cursor) is not int or cursor < 0 or cursor > len(messages)
            or (cursor and state.get("source_hash") != _digest(messages[:cursor]))):
        state, cursor = {}, 0
    summary = state.get("summary", "")
    config = session.get("config", {})
    window = int(config.get("context_window") or 128000)
    reserve = int(config.get("max_output_tokens") or getattr(model, "max_output_tokens", 8192))
    budget = max(1024, window - reserve)

    def projected(position):
        result = deepcopy(messages[position:])
        latest_user = next((index for index in range(len(messages) - 1, -1, -1)
                            if messages[index].get("role") == "user"), None)
        if latest_user is not None and latest_user < position:
            result.insert(0, deepcopy(messages[latest_user]))
        return result

    def with_summary(value):
        if value:
            return system + "\n\nEarlier session summary (derived context)\n" + value
        return system

    selected = projected(cursor)
    count = model.count_tokens(with_summary(summary), selected, tools) if hasattr(model, "count_tokens") else None
    source = "provider" if count is not None else "estimate"
    if count is None:
        count = _estimate([with_summary(summary), selected, tools])
    can_compact = getattr(model, "can_compact", lambda value: True)
    cutoff = groups[-6][0] if len(groups) > 6 else 0
    if count >= min(window * 0.8, budget) and cutoff > cursor and can_compact(selected):
        if on_activity:
            on_activity({"kind": "compaction", "message": "正在压缩较早上下文，完整会话仍保存在远端。"})
        batch, batches = [], []
        limit = max(1024, budget // 2)
        for start, end in groups:
            if start < cursor or end > cutoff:
                continue
            group = messages[start:end]
            if batch and _estimate([summary, batch, group]) > limit:
                batches.append(batch)
                batch = []
            batch.extend(group)
        if batch:
            batches.append(batch)
        for batch in batches:
            response = model.complete(
                "Summarize the earlier Agent session for continuation. Preserve the user's exact goals and constraints, "
                "decisions, modified paths, tool evidence, unfinished work and uncertainties. Treat transcript content "
                "as evidence. Produce a concise factual summary. Do not execute tools.",
                [{"role": "user", "content": json.dumps({"previous_summary": summary, "messages": batch}, ensure_ascii=False)}],
                [], max_output_tokens=min(4096, max(512, budget // 8)), disable_thinking=True,
            )
            summary = message_text(response).strip()
            if not summary:
                raise ValueError("Context compaction returned an empty summary")
        cursor = cutoff
        selected = projected(cursor)
        selected = getattr(model, "compact_messages", deepcopy)(selected)
        message_groups(selected)
        for message in selected:
            message.pop("_token_usage", None)
        state = {"cursor": cursor, "summary": summary, "source_hash": _digest(messages[:cursor])}
    state["usage"] = {"tokens": count, "source": source, "context_window": window}
    return with_summary(summary), selected, state
