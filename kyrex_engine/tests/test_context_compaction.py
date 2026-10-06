import json
from types import SimpleNamespace
from kyrex.core import PlaneExecute


def test_compaction_applies_when_message_count_stays_the_same():
    engine = object.__new__(PlaneExecute)
    call = {"id": "call-1", "type": "function", "function": {"name": "delegate_task", "arguments": "{}"}}
    older = [{"role": "system", "content": "Policy"},
             {"role": "user", "content": "Find game time"},
             {"role": "assistant", "content": "x" * 12000, "tool_calls": [call], "reasoning_content": "Required reasoning"},
             {"role": "tool", "tool_call_id": "call-1", "content": "y" * 20000}]
    tail = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Recent {i}"} for i in range(15)]
    original = older + tail
    engine.session = SimpleNamespace(history=original)
    engine.context_limit = 1000
    engine._compaction_count = 0
    engine._check_context_compaction()
    assert len(engine.session.history) == len(original)
    assert len(json.dumps(engine.session.history)) < len(json.dumps(original))
    assert engine.session.history[-15:] == tail
    assert engine.session.history[0:2] == older[0:2]
    assert engine.session.history[2]["tool_calls"] == [call]
    assert engine.session.history[2]["reasoning_content"] == "Required reasoning"
    assert engine.session.history[3]["tool_call_id"] == "call-1"
    assert engine._compaction_count == 1
    engine._check_context_compaction()
    assert engine._compaction_count == 1, "unchanged compaction should not be counted repeatedly"
