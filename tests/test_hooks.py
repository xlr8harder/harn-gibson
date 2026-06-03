from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harn_gibson.events import GibsonEvent
from harn_gibson.hooks import HookDecision, HookDispatcher, load_hook_module, result_for_harn


def event(event_type: str, phase: str | None = None) -> GibsonEvent:
    payload = {"type": event_type}
    return GibsonEvent.from_raw(payload, 1, timestamp_ms=1) if phase is None else GibsonEvent(
        sequence=1,
        timestamp_ms=1,
        source="test",
        event_type=event_type,
        phase=phase,  # type: ignore[arg-type]
        title=event_type,
        summary="summary",
        payload=payload,
    )


def test_hook_decision_from_mapping_and_none() -> None:
    assert HookDecision.from_result(None) is None
    existing = HookDecision(block=True)
    assert HookDecision.from_result(existing) is existing

    decision = HookDecision.from_result(
        {
            "block": True,
            "reason": "no",
            "action": "transform",
            "replacement": {"text": "new"},
            "display": False,
            "metadata": {"k": "v"},
        }
    )
    assert decision == HookDecision(
        block=True,
        reason="no",
        action="transform",
        replacement={"text": "new"},
        display=False,
        metadata={"k": "v"},
    )
    assert decision.to_dict()["reason"] == "no"

    invalid = HookDecision.from_result({"reason": 1, "action": "skip"})
    assert invalid == HookDecision()


def test_dispatcher_order_and_async_handlers() -> None:
    dispatcher = HookDispatcher()
    calls: list[str] = []

    @dispatcher.on()
    def all_events(_event: GibsonEvent) -> None:
        calls.append("all")

    @dispatcher.on("tool_call")
    async def typed(_event: GibsonEvent) -> HookDecision:
        calls.append("typed")
        await asyncio.sleep(0)
        return HookDecision(metadata={"typed": True})

    def phased(_event: GibsonEvent) -> dict[str, object]:
        calls.append("phase")
        return {"metadata": {"phase": True}}

    dispatcher.on("*", "before", phased)

    @dispatcher.on("tool_call", "before")
    def exact(_event: GibsonEvent) -> HookDecision:
        calls.append("exact")
        return HookDecision(block=True, reason="stop")

    decisions = asyncio.run(dispatcher.dispatch(event("tool_call")))

    assert calls == ["all", "typed", "phase", "exact"]
    assert [decision.metadata for decision in decisions] == [{"typed": True}, {"phase": True}, {}]
    assert dispatcher.handlers_for(event("agent_end")) == [all_events]


def test_result_for_harn_mutable_events() -> None:
    block = [HookDecision(block=True, reason="policy")]
    assert result_for_harn("session_before_switch", block) == {"cancel": True}
    assert result_for_harn("tool_call", block) == {"block": True, "reason": "policy"}
    assert result_for_harn("tool_call", [HookDecision(block=True)]) == {
        "block": True,
        "reason": "Blocked by harn-gibson hook",
    }
    assert result_for_harn("input", [HookDecision(action="handled")]) == {"action": "handled"}
    assert result_for_harn("input", [HookDecision(action="transform", replacement={"text": "new"})]) == {
        "action": "transform",
        "text": "new",
    }
    assert result_for_harn(
        "input",
        [HookDecision(action="transform", replacement={"text": "old"}), HookDecision(action="continue")],
    ) == {"action": "transform", "text": "old"}
    assert result_for_harn("input", []) is None
    assert result_for_harn("tool_result", [HookDecision(replacement={"content": [], "isError": True})]) == {
        "content": [],
        "isError": True,
    }
    assert result_for_harn("message_end", [HookDecision(replacement={"message": {"role": "assistant"}})]) == {
        "message": {"role": "assistant"}
    }
    assert result_for_harn("before_agent_start", [HookDecision(replacement={"systemPrompt": "override"})]) == {
        "systemPrompt": "override"
    }
    assert result_for_harn("before_provider_request", [HookDecision(replacement={"payload": {"x": 1}})]) == {"x": 1}
    assert result_for_harn("before_provider_request", []) is None
    assert result_for_harn("agent_end", block) is None


def test_load_hook_module_success_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = tmp_path / "hook.py"
    hook.write_text(
        "from harn_gibson import HookDecision\n"
        "def register_gibson_hooks(dispatcher):\n"
        "    dispatcher.on('input', 'before', lambda event: HookDecision(action='handled'))\n",
        encoding="utf-8",
    )
    dispatcher = HookDispatcher()
    load_hook_module(hook, dispatcher)
    assert result_for_harn("input", asyncio.run(dispatcher.dispatch(event("input")))) == {"action": "handled"}

    bad = tmp_path / "bad.py"
    bad.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="register_gibson_hooks"):
        load_hook_module(bad, HookDispatcher())

    monkeypatch.setattr("importlib.util.spec_from_file_location", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError, match="Cannot load"):
        load_hook_module(bad, HookDispatcher())
