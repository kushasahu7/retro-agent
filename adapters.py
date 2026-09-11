"""Agent adapters: every supported coding agent is normalised into one IR.

The IR's load-bearing idea is `kind`, not `name`. Claude Code names its tools
(Edit, Bash); Codex routes nearly everything through `shell`. Metrics must not
care, so each adapter is responsible for saying what a call actually DID.
"""
import json, os, re, glob, gzip
import datetime as dt
from collections import Counter

KINDS = ("edit", "read", "shell", "verify", "search", "web", "other")

VERIFY_RE = re.compile(
    r"\b(pytest|npm (run )?(test|lint|build)|yarn (test|lint)|pnpm (test|lint|build)"
    r"|go test|cargo (test|check|clippy)|ruff|mypy|tsc|eslint|jest|vitest"
    r"|make (test|check)|python -m (pytest|unittest)|git diff|git status)\b", re.I)
# `sed` is listed here deliberately: the in-place case is matched earlier and
# returns before this, so anything reaching here is a print like `sed -n`.
READ_RE = re.compile(r"^\s*(cat|head|tail|less|nl|rg|grep|ls|find|wc|jq|stat|file"
                     r"|tree|which|ps|du|sed|awk|diff|od|xxd)\b")
PATCH_RE = re.compile(r"\bapply_?patch\b", re.I)
SEDI_RE = re.compile(r"\bsed\s+-i")
TEE_RE = re.compile(r"\btee\b\s+(?!-)([\w./~-]+)")
REDIR_RE = re.compile(r"(?<![0-9&])>>?\s*([\w./~-]+)")
NOT_A_FILE = re.compile(r"^/dev/|^/tmp/|/dev/null$")

def _looks_like_file(p):
    """A redirect target only counts as an edit if it names a real-looking file."""
    if not p or NOT_A_FILE.search(p): return False
    base = os.path.basename(p)
    return ("/" in p and "." in base) or re.match(r"^[\w-]+\.[A-Za-z0-9]{1,6}$", base) is not None
PATCH_PATH = re.compile(r"\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)")

def classify_shell(cmd):
    """Return (kind, target_path_or_None) for a shell command string."""
    if not cmd: return ("shell", None)
    c = str(cmd)
    # Strip the `bash -lc "..."` wrapper Codex uses.
    m = re.match(r"^\s*(?:bash|sh|zsh)\s+-l?c\s+(.*)$", c, re.S)
    if m: c = m.group(1).strip().strip("'\"")
    if VERIFY_RE.search(c): return ("verify", None)
    if PATCH_RE.search(c):
        p = PATCH_PATH.search(c)
        return ("edit", p.group(1).strip() if p else None)
    if SEDI_RE.search(c):
        # The file is the last argument. Scan from the right for the first token
        # that looks like a path, so the sed script and an empty -i suffix are
        # both skipped regardless of how many arguments precede the file.
        toks = [t.strip("'\"") for t in c.split()]
        return ("edit", next((t for t in reversed(toks) if _looks_like_file(t)), None))
    m3 = TEE_RE.search(c)
    if m3 and _looks_like_file(m3.group(1)): return ("edit", m3.group(1))
    m4 = REDIR_RE.search(c)
    if m4 and _looks_like_file(m4.group(1)): return ("edit", m4.group(1))
    if READ_RE.match(c): return ("read", None)
    return ("shell", None)

def parse_ts(v):
    if not v: return None
    if isinstance(v, (int, float)):
        try: return dt.datetime.fromtimestamp(v/1000 if v > 1e11 else v).astimezone()
        except Exception: return None
    try: return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception: return None

def _open(path):
    return gzip.open(path, "rt", errors="replace") if path.endswith(".gz") \
           else open(path, "r", errors="replace")


def _lines(path):
    """Iterate a transcript and close the handle when done.

    Iterating `_open(path)` directly leaked one descriptor per session, which a
    163-session scan turns into 163 open files.
    """
    with _open(path) as fh:
        for line in fh:
            yield line

class SessionIR:
    def __init__(self, agent, path):
        self.agent = agent; self.path = path
        self.sid = None; self.project = None; self.title = None; self.branch = None
        self.turns = []; self.tools = []; self.tokens = Counter()
        self.compactions = 0; self.unknown_types = Counter()
        self.cumulative_tokens = False

# ------------------------------------------------------------------ Claude Code
CLAUDE_EDIT = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CLAUDE_READ = {"Read", "Glob", "Grep", "NotebookRead"}
CLAUDE_WEB = {"WebFetch", "WebSearch"}
CLAUDE_KNOWN = {"ai-title","mode","permission-mode","last-prompt","attachment","system",
                "file-history-snapshot","file-history-delta","queue-operation",
                "bridge-session","atis-latch","cost-state"}

class ClaudeAdapter:
    name = "claude-code"
    @staticmethod
    def discover(root=None):
        root = root or os.environ.get("RETRO_PROJECTS") or os.path.expanduser("~/.claude/projects")
        return sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))
    @staticmethod
    def matches(path):
        return "/.claude/" in path or "/projects/" in path
    @staticmethod
    def classify(name, inp):
        if name in CLAUDE_EDIT: return ("edit", inp.get("file_path"))
        if name in CLAUDE_READ: return ("read", inp.get("file_path"))
        if name in CLAUDE_WEB: return ("web", inp.get("url"))
        if name == "Bash": return classify_shell(inp.get("command"))
        return ("other", None)

    @classmethod
    def load(cls, path):
        s = SessionIR(cls.name, path)
        s.sid = os.path.basename(path).split(".")[0]
        s.project = os.path.basename(os.path.dirname(path))
        pending = {}; idx = 0
        for line in _lines(path):
            line = line.strip()
            if not line: continue
            try: d = json.loads(line)
            except Exception: continue
            t = d.get("type")
            if t == "ai-title" and not s.title: s.title = d.get("aiTitle")
            if d.get("gitBranch") and not s.branch: s.branch = d.get("gitBranch")
            if d.get("subtype") in ("compact_boundary", "compact"): s.compactions += 1
            if t not in ("user", "assistant"):
                if t not in CLAUDE_KNOWN: s.unknown_types[t] += 1
                continue
            msg = d.get("message") or {}
            ts = parse_ts(d.get("timestamp"))
            side = bool(d.get("isSidechain")); meta = bool(d.get("isMeta"))
            u = msg.get("usage") if isinstance(msg, dict) else None
            if u:
                for k in ("input_tokens","output_tokens",
                          "cache_read_input_tokens","cache_creation_input_tokens"):
                    s.tokens[k] += u.get(k) or 0
            c = msg.get("content")
            bs = c if isinstance(c, list) else []
            text = c if isinstance(c, str) else "".join(
                b.get("text","") for b in bs if isinstance(b, dict) and b.get("type")=="text")
            has_res = False
            for b in bs:
                if not isinstance(b, dict): continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    kind, tgt = cls.classify(b.get("name"), inp)
                    e = dict(idx=idx, name=b.get("name"), kind=kind, target=tgt,
                             input=inp, ts=ts, sidechain=side, error=None, id=b.get("id"))
                    s.tools.append(e)
                    if b.get("id"): pending[b["id"]] = e
                elif b.get("type") == "tool_result":
                    has_res = True
                    e = pending.get(b.get("tool_use_id"))
                    if e is not None: e["error"] = bool(b.get("is_error"))
            if t == "user" and (has_res or meta) and not str(text).strip(): continue
            s.turns.append(dict(idx=idx, role=t, ts=ts, sidechain=side, text=text,
                                chars=len(text or ""), uuid=d.get("uuid"),
                                parent=d.get("parentUuid"), meta=meta))
            idx += 1
        return s

# ------------------------------------------------------------------ Codex
IDE_REQ = re.compile(r"##\s*(?:My request for Codex|My request|Request)\s*:\s*\n?(.*)$",
                     re.S | re.I)

def strip_ide_context(text):
    """Codex IDE sessions wrap the real prompt in an editor-context block.
    Return (prompt, is_meta): the prompt if one is embedded, else meta=True."""
    t = text or ""
    if "<environment_context>" in t: return ("", True)
    head = t.lstrip()[:60]
    if head.startswith("# Context from my IDE") or head.startswith("# Files mentioned by the"):
        m = IDE_REQ.search(t)
        if m and m.group(1).strip(): return (m.group(1).strip(), False)
        return ("", True)
    return (t, False)

class CodexAdapter:
    name = "codex"
    @staticmethod
    def discover(root=None):
        root = root or os.environ.get("RETRO_CODEX") or os.path.expanduser("~/.codex/sessions")
        return sorted(glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")))
    @staticmethod
    def matches(path):
        return ("/.codex/" in path or "/codex/" in path
                or os.path.basename(path).startswith("rollout-"))

    @classmethod
    def load(cls, path):
        s = SessionIR(cls.name, path)
        s.cumulative_tokens = True
        b = os.path.basename(path)
        s.sid = b[:-6].split("-", 1)[1] if b.startswith("rollout-") else b[:-6]
        pending = {}; idx = 0; last_usage = None; last_ts = None; seen_text = set()
        for line in _lines(path):
            line = line.strip()
            if not line: continue
            try: d = json.loads(line)
            except Exception: continue
            # Two format generations: newer records nest under `payload`.
            n = d.get("payload") if isinstance(d.get("payload"), dict) else d
            ty = n.get("type") or d.get("type")
            ts = parse_ts(d.get("timestamp") or n.get("timestamp"))
            if ts: last_ts = ts
            else: ts = last_ts

            if ty == "session_meta" or n.get("session_id"):
                s.sid = n.get("session_id") or s.sid
                cwd = n.get("cwd")
                if cwd and not s.project: s.project = os.path.basename(cwd)
                continue
            if ty == "token_count":
                tu = (n.get("info") or {}).get("total_token_usage")
                if isinstance(tu, dict): last_usage = tu   # cumulative, keep the last
                continue
            if ty == "function_call":
                try: a = json.loads(n.get("arguments") or "{}")
                except Exception: a = {}
                cmd = a.get("command")
                if isinstance(cmd, list): cmd = " ".join(map(str, cmd))
                if n.get("name") == "shell":
                    kind, tgt = classify_shell(cmd)
                else:
                    kind, tgt = ("other", None)
                e = dict(idx=idx, name=n.get("name") or "shell", kind=kind, target=tgt,
                         input={"command": cmd} if cmd else a, ts=ts, sidechain=False,
                         error=None, id=n.get("call_id"))
                s.tools.append(e)
                if n.get("call_id"): pending[n["call_id"]] = e
                continue
            if ty == "function_call_output":
                e = pending.get(n.get("call_id"))
                o = n.get("output")
                if isinstance(o, str):
                    try: o = json.loads(o)
                    except Exception: o = {"raw": o}
                code = None
                if isinstance(o, dict) and isinstance(o.get("metadata"), dict):
                    code = o["metadata"].get("exit_code")
                if e is not None:
                    e["error"] = (code not in (0, None)) if code is not None else False
                continue
            # Newer Codex emits the real prompt/reply as their own record types.
            if ty in ("user_message", "agent_message"):
                role = "user" if ty == "user_message" else "assistant"
                text = n.get("message") or n.get("text") or ""
                key = (role, " ".join(str(text).split())[:200])
                if key in seen_text: continue
                seen_text.add(key)
                if role == "user" and not s.title and str(text).strip():
                    s.title = " ".join(str(text).split())[:60]
                s.turns.append(dict(idx=idx, role=role, ts=ts, sidechain=False,
                                    text=text, chars=len(str(text)), uuid=n.get("id"),
                                    parent=None, meta=False))
                idx += 1
                continue
            if ty == "message":
                role = n.get("role")
                c = n.get("content")
                text = c if isinstance(c, str) else "".join(
                    x.get("text","") for x in (c or []) if isinstance(x, dict))
                # Codex injects an environment_context block as a fake user turn.
                if role == "user":
                    text, meta = strip_ide_context(text)
                else:
                    meta = False
                if role not in ("user", "assistant"): continue
                key = (role, " ".join((text or "").split())[:200])
                if key in seen_text: continue
                if not meta: seen_text.add(key)
                if not s.title and role == "user" and not meta and text.strip():
                    s.title = " ".join(text.split())[:60]
                s.turns.append(dict(idx=idx, role=role, ts=ts, sidechain=False,
                                    text=text, chars=len(text or ""), uuid=n.get("id"),
                                    parent=None, meta=meta))
                idx += 1
                continue
            # Newer Codex runs shell through custom_tool_call: the command is
            # embedded in a JS snippet, tools.exec_command({cmd:"..."}).
            if ty == "custom_tool_call":
                inp = n.get("input") or ""
                mm = re.search(r'exec_command\(\{\s*cmd\s*:\s*"((?:[^"\\]|\\.)*)"', str(inp))
                cmd = mm.group(1).encode().decode("unicode_escape") if mm else None
                kind, tgt = classify_shell(cmd) if cmd else ("other", None)
                e = dict(idx=idx, name=n.get("name") or "exec", kind=kind, target=tgt,
                         input={"command": cmd} if cmd else {"raw": str(inp)[:400]},
                         ts=ts, sidechain=False, error=None, id=n.get("call_id"))
                s.tools.append(e)
                if n.get("call_id"): pending[n["call_id"]] = e
                continue
            if ty == "custom_tool_call_output":
                e = pending.get(n.get("call_id"))
                o = n.get("output")
                txt = o if isinstance(o, str) else " ".join(
                    x.get("text", "") for x in (o or []) if isinstance(x, dict))
                if e is not None:
                    # No exit code in this record type; only obvious failures are visible.
                    e["error"] = bool(re.search(
                        r"command not found|No such file or directory|Traceback \(most recent"
                        r"|error:|failed with exit", txt, re.I))
                continue
            if ty == "patch_apply_end":
                ch = n.get("changes") or {}
                ok = bool(n.get("success"))
                for fp in (ch.keys() if isinstance(ch, dict) else []):
                    s.tools.append(dict(idx=idx, name="apply_patch", kind="edit", target=fp,
                                        input={}, ts=ts, sidechain=False,
                                        error=not ok, id=n.get("call_id")))
                continue
            if ty == "web_search_end":
                s.tools.append(dict(idx=idx, name="web_search", kind="web",
                                    target=n.get("query"), input={}, ts=ts,
                                    sidechain=False, error=False, id=n.get("call_id")))
                continue
            if ty in ("reasoning", "response_item", "event_msg", "turn_context",
                      "compacted", "task_started",
                      "task_complete", "world_state", "thread_settings_applied",
                      "agent_reasoning", "image_generation_end", "patch_apply_begin",
                      "web_search_begin", "exec_command_begin", "exec_command_end"):
                if ty == "compacted": s.compactions += 1
                continue
            if ty: s.unknown_types[ty] += 1
        if last_usage:
            s.tokens["input_tokens"] = last_usage.get("input_tokens", 0)
            s.tokens["output_tokens"] = last_usage.get("output_tokens", 0)
            s.tokens["cache_read_input_tokens"] = last_usage.get("cached_input_tokens", 0)
            s.tokens["cache_creation_input_tokens"] = last_usage.get("cache_write_input_tokens", 0)
        return s


# ------------------------------------------------------------------ Cursor
CURSOR_DB = os.environ.get("RETRO_CURSOR_DB") or os.path.expanduser(
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
CURSOR_EDIT = {"edit_file", "edit_file_v2", "search_replace", "create_file",
               "delete_file", "write_file", "apply_patch", "multi_edit"}
CURSOR_READ = {"read_file", "read_file_v2", "read_lints", "list_dir"}
CURSOR_SEARCH = {"grep_search", "ripgrep_raw_search", "glob_file_search",
                 "codebase_search", "file_search"}
CURSOR_SHELL = {"run_terminal_cmd", "run_terminal_command_v2"}
CURSOR_WEB = {"web_search", "fetch_rules"}

_CURSOR_CONNS = {}

def _cursor_conn(db=None):
    """One read-only connection per DB path, reused. Opening 145 connections and
    re-scanning the KV table per session was the whole cost of a Cursor scan."""
    import sqlite3
    path = db or CURSOR_DB
    if path in _CURSOR_CONNS: return _CURSOR_CONNS[path]
    if not os.path.exists(path): return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    try:
        con.execute("CREATE INDEX IF NOT EXISTS x ON cursorDiskKV(key)")
    except Exception:
        pass  # read-only store; the LIKE prefix scan still uses the PK when present
    _CURSOR_CONNS[path] = con
    return con

class CursorAdapter:
    name = "cursor"

    @staticmethod
    def discover(db=None):
        con = _cursor_conn(db)
        if con is None: return []
        try:
            rows = con.execute(
                "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%' "
                "AND value IS NOT NULL AND length(value) > 50").fetchall()
        except Exception: return []
        finally: pass
        return sorted("cursor://" + k.split(":", 1)[1] for (k,) in rows)

    @staticmethod
    def matches(path):
        return str(path).startswith("cursor://") or (os.sep + "cursor" + os.sep) in str(path)

    @staticmethod
    def _target(params, raw):
        for src, keys in ((params, ("relativeWorkspacePath", "targetFile", "path")),
                          (raw, ("file_path", "target_file", "path"))):
            for k in keys:
                v = src.get(k)
                if v: return v
        return None

    @classmethod
    def classify(cls, name, params, raw):
        if name in CURSOR_EDIT: return ("edit", cls._target(params, raw))
        if name in CURSOR_READ: return ("read", cls._target(params, raw))
        if name in CURSOR_SEARCH: return ("search", None)
        if name in CURSOR_WEB: return ("web", None)
        if name in CURSOR_SHELL:
            return classify_shell(params.get("command") or raw.get("command"))
        return ("other", None)

    @classmethod
    def _records(cls, composer_id, db=None):
        """Pull one conversation out of the KV store as ordered raw records."""
        con = _cursor_conn(db)
        if con is None: return None, []
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                          (f"composerData:{composer_id}",)).fetchone()
        if not row or not row[0]: return None, []
        meta = json.loads(row[0])
        order = [h.get("bubbleId") for h in (meta.get("fullConversationHeadersOnly") or [])
                 if isinstance(h, dict)]
        got = {}
        for k, v in con.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE ? AND value IS NOT NULL",
                (f"bubbleId:{composer_id}:%",)):
            try: got[k.rsplit(":", 1)[1]] = json.loads(v)
            except Exception: continue
        if order:
            bubbles = [got[b] for b in order if b in got]
            # headers can lag behind the store; append anything they missed
            extra = [b for k, b in got.items() if k not in set(order)]
            bubbles += sorted(extra, key=lambda d: d.get("createdAt") or 0)
        else:
            bubbles = sorted(got.values(), key=lambda d: d.get("createdAt") or 0)
        return meta, bubbles

    @classmethod
    def export(cls, ident, db=None, include_code=False):
        """Serialise a KV-stored conversation to JSONL so it can be archived."""
        cid = str(ident).replace("cursor://", "")
        meta, bubbles = cls._records(cid, db)
        if meta is None: return b""
        out = [json.dumps({"_retro": "cursor-meta", "composerId": cid, "meta": {
            k: meta.get(k) for k in ("name", "createdAt", "lastUpdatedAt",
                                     "unifiedMode", "isAgentic", "modelConfig")}})]
        if not include_code:
            import privacy
            bubbles = [privacy.strip_cursor_code(b) for b in bubbles]
        out += [json.dumps({"_retro": "cursor-bubble", "bubble": b}) for b in bubbles]
        return ("\n".join(out) + "\n").encode()

    @classmethod
    def load(cls, path, db=None):
        s = SessionIR(cls.name, path)
        if str(path).startswith("cursor://"):
            cid = str(path).replace("cursor://", "")
            meta, bubbles = cls._records(cid, db)
            if meta is None: return s
        else:  # archived JSONL export
            meta = {}; bubbles = []
            for line in _lines(path):
                line = line.strip()
                if not line: continue
                try: d = json.loads(line)
                except Exception: continue
                if d.get("_retro") == "cursor-meta":
                    meta = d.get("meta") or {}; cid = d.get("composerId")
                elif d.get("_retro") == "cursor-bubble":
                    bubbles.append(d.get("bubble") or {})
            cid = locals().get("cid") or os.path.basename(str(path)).split(".")[0]
        s.sid = cid
        s.title = (meta.get("name") or "").strip() or None
        s.project = None
        idx = 0
        for b in bubbles:
            if not isinstance(b, dict): continue
            ts = parse_ts(b.get("createdAt"))
            btype = b.get("type")
            tf = b.get("toolFormerData")
            if isinstance(tf, dict) and (tf.get("name") or tf.get("tool")):
                nm = tf.get("name") or tf.get("tool")
                def _j(x):
                    if isinstance(x, dict): return x
                    try: return json.loads(x) if x else {}
                    except Exception: return {}
                params, raw = _j(tf.get("params")), _j(tf.get("rawArgs"))
                kind, tgt = cls.classify(nm, params, raw)
                st = (tf.get("status") or "").lower()
                err = None if not st else (st not in ("completed", "success", "done"))
                # Cursor uniquely records whether the human accepted the edit.
                dec = tf.get("userDecision")
                s.tools.append(dict(idx=idx, name=nm, kind=kind, target=tgt,
                                    input=raw or params, ts=ts, sidechain=False,
                                    error=err, id=tf.get("toolCallId"),
                                    decision=dec))
                continue
            text = b.get("text") or ""
            if not str(text).strip(): continue
            role = "user" if btype == 1 else "assistant"
            if role == "user" and not s.title:
                s.title = " ".join(str(text).split())[:60]
            s.turns.append(dict(idx=idx, role=role, ts=ts, sidechain=False, text=text,
                                chars=len(str(text)), uuid=b.get("bubbleId"),
                                parent=None, meta=False))
            idx += 1
        return s

ADAPTERS = [ClaudeAdapter, CodexAdapter, CursorAdapter]

def adapter_for(path):
    for a in ADAPTERS:
        if a.matches(path): return a
    return ClaudeAdapter

def load_any(path):
    return adapter_for(path).load(path)

def discover_all():
    out = []
    for a in ADAPTERS:
        try: out += [(a, p) for p in a.discover()]
        except Exception: pass
    return out
