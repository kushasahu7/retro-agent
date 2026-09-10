#!/usr/bin/env python3
"""Generate a synthetic session corpus so screenshots and demos contain no real data.

Writes Claude Code and Codex shaped transcripts with deliberate friction patterns:
one clean session, one that edits a file repeatedly without ever verifying, one
that flails on a broken environment.
"""
import json, os, sys, uuid, shutil
import datetime as dt

def ts(base, mins, secs=0):
    return (base + dt.timedelta(minutes=mins, seconds=secs)).isoformat()

def claude_session(path, title, base, script):
    """script: list of (kind, payload). kind in user|text|tool|result"""
    recs = [{"type": "ai-title", "aiTitle": title, "sessionId": os.path.basename(path)[:-6]}]
    t = 0
    for kind, payload in script:
        t += 1
        if kind == "user":
            recs.append({"type": "user", "timestamp": ts(base, t), "uuid": str(uuid.uuid4()),
                         "gitBranch": "main", "isSidechain": False,
                         "message": {"role": "user", "content": payload}})
        elif kind == "tool":
            name, inp, is_err = payload
            cid = "t" + uuid.uuid4().hex[:8]
            recs.append({"type": "assistant", "timestamp": ts(base, t), "uuid": str(uuid.uuid4()),
                         "isSidechain": False,
                         "message": {"role": "assistant",
                                     "usage": {"input_tokens": 120, "output_tokens": 340,
                                               "cache_read_input_tokens": 18000,
                                               "cache_creation_input_tokens": 900},
                                     "content": [{"type": "tool_use", "id": cid,
                                                  "name": name, "input": inp}]}})
            recs.append({"type": "user", "timestamp": ts(base, t, 5), "isSidechain": False,
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": cid,
                              "is_error": is_err,
                              "content": "error: command failed" if is_err else "ok"}]}})
    with open(path, "w") as fh:
        for r in recs: fh.write(json.dumps(r) + "\n")

def codex_session(path, base, cmds):
    recs = [{"type": "session_meta", "timestamp": ts(base, 0),
             "payload": {"session_id": os.path.basename(path)[8:-6], "id": "x",
                         "cwd": "/home/dev/acme-api", "cli_version": "0.150.0",
                         "originator": "codex_cli"}}]
    recs.append({"type": "user_message", "timestamp": ts(base, 0),
                 "payload": {"type": "user_message",
                             "message": "add retry logic to the ingest worker"}})
    for i, (cmd, code) in enumerate(cmds):
        cid = "call_" + uuid.uuid4().hex[:10]
        recs.append({"type": "response_item", "timestamp": ts(base, i + 1),
                     "payload": {"type": "function_call", "name": "shell",
                                 "call_id": cid,
                                 "arguments": json.dumps({"command": ["bash", "-lc", cmd]})}})
        recs.append({"type": "response_item", "timestamp": ts(base, i + 1, 8),
                     "payload": {"type": "function_call_output", "call_id": cid,
                                 "output": json.dumps({"output": "...",
                                                       "metadata": {"exit_code": code}})}})
    recs.append({"type": "token_count", "timestamp": ts(base, len(cmds) + 1),
                 "payload": {"type": "token_count", "info": {"total_token_usage": {
                     "input_tokens": 4200, "cached_input_tokens": 31000,
                     "cache_write_input_tokens": 800, "output_tokens": 1900,
                     "reasoning_output_tokens": 300, "total_tokens": 38200}}}})
    with open(path, "w") as fh:
        for r in recs: fh.write(json.dumps(r) + "\n")

def fake_history(path, days=365, seed=7):
    """A year of synthetic prompt history so `retro heatmap` has something to draw
    without touching real data. Bursty, weekday-weighted, with quiet stretches."""
    import random
    rnd = random.Random(seed)
    today = dt.date.today()
    out = []
    streak = 0
    for i in range(days):
        d = today - dt.timedelta(days=days - 1 - i)
        weekday = d.weekday()
        base = 0.62 if weekday < 5 else 0.18      # weekends are quieter, not empty
        if streak > 0:                            # projects arrive in bursts
            base += 0.25
            streak -= 1
        elif rnd.random() < 0.06:
            streak = rnd.randint(3, 12)
        if rnd.random() > base:
            continue
        n = max(1, int(rnd.lognormvariate(2.1, 0.8)))
        for k in range(n):
            when = dt.datetime.combine(d, dt.time(9 + rnd.randint(0, 11),
                                                  rnd.randint(0, 59)))
            out.append({"display": rnd.choice([
                "fix the failing test", "add retry logic", "why is this slow",
                "refactor the worker", "write a migration", "explain this stack trace",
                "add pagination", "tidy up the imports", "make it responsive"]),
                "pastedContents": {}, "project": "/home/dev/acme-api",
                "sessionId": f"demo-{d.isoformat()}-{k}",
                "timestamp": int(when.timestamp() * 1000)})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in sorted(out, key=lambda x: x["timestamp"]):
            fh.write(json.dumps(r) + "\n")
    return len(out)


def build(root):
    shutil.rmtree(root, ignore_errors=True)
    cp = os.path.join(root, "projects", "-home-dev-acme-api")
    cp2 = os.path.join(root, "projects", "-home-dev-demo-site")
    cx = os.path.join(root, "codex", "sessions", "2026", "09", "02")
    for d in (cp, cp2, cx): os.makedirs(d, exist_ok=True)
    base = dt.datetime(2026, 9, 2, 10, 0).astimezone()

    # 1. Healthy session: edits are verified.
    claude_session(os.path.join(cp, str(uuid.uuid4()) + ".jsonl"),
        "Add pagination to the alerts endpoint", base, [
        ("user", "add cursor pagination to /alerts"),
        ("tool", ("Read", {"file_path": "/home/dev/acme-api/routes/alerts.py"}, False)),
        ("tool", ("Edit", {"file_path": "/home/dev/acme-api/routes/alerts.py"}, False)),
        ("tool", ("Bash", {"command": "pytest tests/test_alerts.py -q"}, False)),
        ("tool", ("Edit", {"file_path": "/home/dev/acme-api/routes/alerts.py"}, False)),
        ("tool", ("Bash", {"command": "pytest tests/test_alerts.py -q"}, False)),
        ("user", "ship it"),
    ])

    # 2. Churn: same file edited over and over, never verified.
    script = [("user", "make the hero section responsive")]
    for i in range(9):
        script.append(("tool", ("Edit", {"file_path": "/home/dev/demo-site/src/Hero.jsx"}, False)))
        if i in (3, 6):
            script.append(("user", "still not right on mobile"))
        script.append(("tool", ("Bash", {"command": "cat src/Hero.jsx"}, False)))
    claude_session(os.path.join(cp2, str(uuid.uuid4()) + ".jsonl"),
                   "Make the hero section responsive", base + dt.timedelta(hours=2), script)

    # 3. Flail: broken environment, repeated identical calls, error clusters.
    script = [("user", "the worker will not start locally")]
    for i in range(5):
        script.append(("tool", ("Bash", {"command": "docker compose up worker"}, True)))
    script.append(("user", "try installing the deps first"))
    for i in range(3):
        script.append(("tool", ("Bash", {"command": "pip install -r requirements.txt"}, i < 2)))
    script.append(("tool", ("Edit", {"file_path": "/home/dev/acme-api/docker-compose.yml"}, False)))
    script.append(("tool", ("Bash", {"command": "docker compose up worker"}, False)))
    claude_session(os.path.join(cp, str(uuid.uuid4()) + ".jsonl"),
                   "Debug worker failing to start", base + dt.timedelta(hours=5), script)

    # 4. Codex: shell-only tools, apply_patch edits.
    codex_session(os.path.join(cx, f"rollout-2026-09-02T14-05-00-{uuid.uuid4()}.jsonl"),
        base + dt.timedelta(hours=4), [
        ("rg --files -n", 0),
        ("sed -n '1,80p' worker/ingest.py", 0),
        ("apply_patch <<'EOF'\n*** Update File: worker/ingest.py\nEOF", 0),
        ("apply_patch <<'EOF'\n*** Update File: worker/ingest.py\nEOF", 0),
        ("apply_patch <<'EOF'\n*** Update File: worker/ingest.py\nEOF", 0),
        ("python -m pytest tests/ -q", 1),
        ("apply_patch <<'EOF'\n*** Update File: worker/ingest.py\nEOF", 0),
        ("python -m pytest tests/ -q", 0),
    ])
    n = fake_history(os.path.join(root, "claude", "history.jsonl"))
    print(f"  synthetic prompt history: {n} prompts over a year")
    return root

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "/tmp/retro-demo"
    build(root)
    print(f"demo corpus written to {root}")
