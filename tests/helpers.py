"""Fixture builders for the test suite.

Every fixture is synthetic. Nothing here reads a real session store, so the
suite is safe to run on any machine and its results do not depend on whatever
happens to be in the author's home directory.
"""
import json, os, sqlite3, sys, uuid
import datetime as dt

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

BASE = dt.datetime(2026, 9, 2, 10, 0, tzinfo=dt.timezone.utc)


def _ts(mins, secs=0):
    return (BASE + dt.timedelta(minutes=mins, seconds=secs)).isoformat()


# ------------------------------------------------------------------ Claude Code
def claude_session(path, script, title="Test session"):
    """script: list of ("user", text) or ("tool", (name, input_dict, is_error))."""
    recs = [{"type": "ai-title", "aiTitle": title,
             "sessionId": os.path.basename(path)[:-6]}]
    for t, (kind, payload) in enumerate(script, start=1):
        if kind == "user":
            recs.append({"type": "user", "timestamp": _ts(t), "uuid": str(uuid.uuid4()),
                         "isSidechain": False, "gitBranch": "main",
                         "message": {"role": "user", "content": payload}})
        elif kind == "tool":
            name, inp, err = payload
            cid = "t" + uuid.uuid4().hex[:8]
            recs.append({"type": "assistant", "timestamp": _ts(t),
                         "uuid": str(uuid.uuid4()), "isSidechain": False,
                         "message": {"role": "assistant",
                                     "usage": {"input_tokens": 10, "output_tokens": 20,
                                               "cache_read_input_tokens": 500,
                                               "cache_creation_input_tokens": 5},
                                     "content": [{"type": "tool_use", "id": cid,
                                                  "name": name, "input": inp}]}})
            recs.append({"type": "user", "timestamp": _ts(t, 5), "isSidechain": False,
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": cid,
                              "is_error": err, "content": "boom" if err else "ok"}]}})
        else:
            raise ValueError(kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return path


# ------------------------------------------------------------------ Codex
def codex_session(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


def codex_meta(session_id="s1", cwd="/home/dev/acme-api"):
    return {"type": "session_meta", "timestamp": _ts(0),
            "payload": {"session_id": session_id, "cwd": cwd,
                        "cli_version": "0.150.0"}}


def codex_shell(call_id, cmd, exit_code=0, minute=1):
    """A function_call/function_call_output pair, the older Codex shape."""
    return [
        {"type": "response_item", "timestamp": _ts(minute),
         "payload": {"type": "function_call", "name": "shell", "call_id": call_id,
                     "arguments": json.dumps({"command": ["bash", "-lc", cmd]})}},
        {"type": "response_item", "timestamp": _ts(minute, 8),
         "payload": {"type": "function_call_output", "call_id": call_id,
                     "output": json.dumps({"output": "...",
                                           "metadata": {"exit_code": exit_code}})}},
    ]


def codex_custom_exec(call_id, cmd, minute=1, failed=False):
    """The newer custom_tool_call shape: the command hides inside a JS snippet."""
    return [
        {"type": "response_item", "timestamp": _ts(minute),
         "payload": {"type": "custom_tool_call", "name": "exec", "call_id": call_id,
                     "input": 'const r = await tools.exec_command({cmd:"%s",'
                              '"yield_time_ms":10000});' % cmd}},
        {"type": "response_item", "timestamp": _ts(minute, 4),
         "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                     "output": [{"type": "input_text",
                                 "text": "error: command not found" if failed
                                         else "Script completed"}]}},
    ]


def codex_token_count(total_in=4200, total_out=1900, cached=31000, minute=9):
    return {"type": "token_count", "timestamp": _ts(minute),
            "payload": {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": total_in, "output_tokens": total_out,
                "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                "reasoning_output_tokens": 0, "total_tokens": 0}}}}


IDE_WRAPPED = ("# Context from my IDE setup:\n\n"
               "## Active file: src/Hero.jsx\n\n"
               "## Open tabs:\n- Hero.jsx\n\n"
               "## My request for Codex:\n{}")


# ------------------------------------------------------------------ Cursor
def cursor_db(path, conversations):
    """conversations: {composer_id: (name, [bubble dicts])}."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    for cid, (name, bubbles) in conversations.items():
        headers = [{"bubbleId": b["bubbleId"], "type": b.get("type", 2)}
                   for b in bubbles]
        con.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                    (f"composerData:{cid}", json.dumps({
                        "composerId": cid, "name": name,
                        "createdAt": int(BASE.timestamp() * 1000),
                        "unifiedMode": "agent", "isAgentic": True,
                        "fullConversationHeadersOnly": headers})))
        for b in bubbles:
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                        (f"bubbleId:{cid}:{b['bubbleId']}", json.dumps(b)))
    con.commit(); con.close()
    return path


def cursor_text(bid, text, role_type=1, minute=1):
    return {"bubbleId": bid, "type": role_type, "text": text,
            "createdAt": int((BASE + dt.timedelta(minutes=minute)).timestamp() * 1000)}


def cursor_tool(bid, name, params=None, raw=None, status="completed",
                decision=None, minute=1):
    tf = {"name": name, "tool": name, "toolCallId": bid, "status": status,
          "params": params or {}, "rawArgs": raw or {}}
    if decision is not None:
        tf["userDecision"] = decision
    return {"bubbleId": bid, "type": 2, "text": "", "toolFormerData": tf,
            "createdAt": int((BASE + dt.timedelta(minutes=minute)).timestamp() * 1000)}
