"""Fixture test for measure_skill_run.py: every expected number is known by construction."""
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("m", sys.argv[1] if len(sys.argv) > 1 else HERE.parent / "measure_skill_run.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def asst(mid, ctx, out, content, model="claude-opus-5-5", t="2026-10-23T10:00:00Z"):
    return {"type": "assistant", "timestamp": t, "message": {
        "id": mid, "role": "assistant", "model": model, "content": content,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": ctx - 10 - 5,
                  "cache_creation_input_tokens": 5, "output_tokens": out}}}


skill_text = "x" * 4000                      # -> est 1000 tokens
cmd_text = "<command-name>/pz-wf-demo</command-name>"
rows = [
    {"type": "user", "message": {"role": "user", "content": cmd_text}},
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": [
        {"type": "text", "text": "Base directory for this skill: /x\n" + "y" * 766}]}},
    asst("m1", 20000, 50, [{"type": "text", "text": "Gate 1 starts"}]),
    asst("m1", 20000, 50, [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "kn-a"}}]),
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "Launching skill: kn-a"}]}},
    {"type": "user", "isMeta": True, "sourceToolUseID": "t1",
     "message": {"role": "user", "content": [{"type": "text", "text": skill_text}]}},
    asst("m2", 21100, 70, [{"type": "tool_use", "id": "t2", "name": "Bash",
                            "input": {"command": "cursor-agent -p 'x'", "run_in_background": True}}]),
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "boom"}]}},
    {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"trigger": "auto", "preTokens": 21100, "postTokens": 3000}},
    asst("m3", 4000, 30, [{"type": "text", "text": "Gate 2 now"}], t="2026-10-23T10:30:00Z"),
    asst("m4", 9999, 5, [], model="deepseek-v4"),
    # replayed copies must be ignored
    {"type": "api-request-blob", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t9", "name": "Skill", "input": {"skill": "kn-a"}}]}},
    {"type": "api-request-blob", "message": {"role": "user", "content": cmd_text}},
]
o = m.measure(rows, r"Gate (\d)")
ob = o["observed"]
checks = {
    "requests deduped by message id": ob["requests"] == 4 and ob["assistant_rows"] == 5,
    "peak ctx from Claude only": ob["peak_ctx"] == 21100,
    "final ctx from Claude only": ob["final_ctx"] == 4000,
    "output tokens counted once per message": ob["output_tokens"] == 50 + 70 + 30 + 5,
    "compaction recorded": ob["compactions"] == [{"at_request": 2, "trigger": "auto", "preTokens": 21100, "postTokens": 3000}],
    "background cursor-agent delegation": ob["delegations"][0]["kind"] == "cursor-agent CLI" and ob["delegations"][0]["background"],
    "tool error counted": ob["tool_errors"] == 1,
    "duration": ob["duration_min"] == 30.0,
    "non-claude flagged": ob["non_claude_models"] == ["deepseek-v4"],
    "slash skill detected": o["skills"][0]["skill"] == "pz-wf-demo" and o["skills"][0]["via"] == "slash command",
    "slash skill est tokens": o["skills"][0]["est_tokens"] == round((len(cmd_text) + len("Base directory for this skill: /x\n") + 766) / 4),
    "Skill tool est tokens": o["skills"][1]["est_tokens"] == 1000,
    "Skill tool observed delta": o["skills"][1]["observed_ctx_delta"] == 1100,
    "stages heuristic": [s["stage"] for s in o["stages_heuristic"]] == ["1", "2"] and o["stages_heuristic"][0]["requests"] == 2,
}
bad = [k for k, v in checks.items() if not v]
print(json.dumps(o, indent=1, default=str) if bad else "")
print("\n".join(f"{'PASS' if v else 'FAIL'} {k}" for k, v in checks.items()))
sys.exit(1 if bad else 0)
