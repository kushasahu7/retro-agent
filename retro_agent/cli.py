#!/usr/bin/env python3
"""retro: local-only friction analytics over Claude Code session transcripts.

No network. No model. Deterministic metrics only.
"""
import argparse, json, os, re, sqlite3, sys, glob, hashlib, gzip, shutil, stat
from . import adapters, privacy
from .adapters import classify_shell
import datetime as dt
from collections import Counter, defaultdict

PROJECTS = os.environ.get("RETRO_PROJECTS") or os.path.expanduser("~/.claude/projects")

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
CHECKOUT = os.path.dirname(PKG_DIR)


def _data_root():
    """Where retro keeps its own archive, database and config.

    An installed copy must never write into site-packages, so the default is
    ~/.retro-agent. A git checkout that already holds data next to it keeps
    using that, so installing over an existing clone does not orphan a 46MB
    archive someone has been accumulating.
    """
    env = os.environ.get("RETRO_HOME")
    if env:
        return os.path.expanduser(env)
    for legacy in ("retro.db", "archive", "config.json"):
        if os.path.exists(os.path.join(CHECKOUT, legacy)):
            return CHECKOUT
    return os.path.expanduser("~/.retro-agent")


DATA_ROOT = _data_root()
DB = os.environ.get("RETRO_DB") or os.path.join(DATA_ROOT, "retro.db")
ARCHIVE = os.environ.get("RETRO_ARCHIVE") or os.path.join(DATA_ROOT, "archive")
CLAUDE = os.environ.get("RETRO_CLAUDE") or os.path.expanduser("~/.claude")
# Config sits beside the archive, so redirecting the archive redirects config too.
ROOT = os.path.dirname(ARCHIVE)

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Commands that count as "I checked my work" between edits.
VERIFY_RE = re.compile(
    r"\b(pytest|npm (run )?(test|lint|build)|yarn (test|lint)|go test|cargo (test|check|clippy)"
    r"|ruff|mypy|tsc|eslint|jest|vitest|make (test|check)|python -m (pytest|unittest)"
    r"|git diff|git status)\b", re.I)

def parse_ts(v):
    if not v: return None
    try: return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception: return None

def blocks(msg):
    c = msg.get("content") if isinstance(msg, dict) else None
    return c if isinstance(c, list) else []

def result_text(b):
    c = b.get("content")
    if isinstance(c, str): return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""

class Session:
    def __init__(self, path):
        self.path = path
        self.project = os.path.basename(os.path.dirname(path))
        self.sid = os.path.basename(path)[:-6]
        self.title = None
        self.branch = None
        self.turns = []        # (idx, role, ts, sidechain, text_len, uuid, parent)
        self.tools = []        # dict per tool call
        self.tokens = Counter()
        self.compactions = 0
        self.unknown_types = Counter()

def _legacy_load(path):
    s = Session(path)
    pending = {}   # tool_use_id -> tool dict
    idx = 0
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line: continue
            try: d = json.loads(line)
            except Exception: continue
            t = d.get("type")

            if t == "ai-title" and not s.title:
                s.title = d.get("aiTitle")
            if d.get("gitBranch") and not s.branch:
                s.branch = d.get("gitBranch")
            if d.get("subtype") in ("compact_boundary", "compact"):
                s.compactions += 1

            if t not in ("user", "assistant"):
                if t not in ("ai-title", "mode", "permission-mode", "last-prompt",
                             "attachment", "system", "file-history-snapshot",
                             "file-history-delta", "queue-operation", "bridge-session",
                             "atis-latch", "cost-state"):
                    s.unknown_types[t] += 1
                continue

            msg = d.get("message") or {}
            ts = parse_ts(d.get("timestamp"))
            side = bool(d.get("isSidechain"))
            meta = bool(d.get("isMeta"))

            u = msg.get("usage") if isinstance(msg, dict) else None
            if u:
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens"):
                    s.tokens[k] += u.get(k) or 0

            bs = blocks(msg)
            text = "".join(b.get("text", "") for b in bs
                           if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(msg.get("content"), str):
                text = msg["content"]

            has_tool_result = False
            for b in bs:
                if not isinstance(b, dict): continue
                if b.get("type") == "tool_use":
                    entry = dict(idx=idx, name=b.get("name"), input=b.get("input") or {},
                                 ts=ts, sidechain=side, error=None, id=b.get("id"))
                    s.tools.append(entry)
                    if b.get("id"): pending[b["id"]] = entry
                elif b.get("type") == "tool_result":
                    has_tool_result = True
                    e = pending.get(b.get("tool_use_id"))
                    if e is not None:
                        e["error"] = bool(b.get("is_error"))
                        e["result_len"] = len(result_text(b))

            # A user record that is only a tool_result is not a human turn.
            if t == "user" and (has_tool_result or meta) and not text.strip():
                continue
            s.turns.append(dict(idx=idx, role=t, ts=ts, sidechain=side,
                                chars=len(text), text=text, uuid=d.get("uuid"),
                                parent=d.get("parentUuid"), meta=meta))
            idx += 1
    return s

def load(path):
    return adapters.load_any(path)

def metrics(s):
    m = {}
    main_tools = [t for t in s.tools if not t["sidechain"]]
    sub_tools  = [t for t in s.tools if t["sidechain"]]
    human = [t for t in s.turns if t["role"] == "user" and not t["sidechain"] and not t["meta"]]
    asst  = [t for t in s.turns if t["role"] == "assistant" and not t["sidechain"]]

    ts = [t["ts"] for t in s.turns if t["ts"]]
    ts.sort()
    m["start"], m["end"] = (ts[0], ts[-1]) if ts else (None, None)
    m["wall_min"] = round((ts[-1] - ts[0]).total_seconds() / 60, 1) if len(ts) > 1 else 0.0
    # Active time ignores gaps over 5 minutes (you walked away).
    active = 0.0; gaps = []
    for a, b in zip(ts, ts[1:]):
        g = (b - a).total_seconds()
        if g <= 300: active += g
        elif g >= 600: gaps.append(round(g / 60))
    m["active_min"] = round(active / 60, 1)
    m["idle_gaps"] = gaps

    m["human_turns"] = len(human)
    m["asst_turns"] = len(asst)
    m["sub_tools"] = len(sub_tools)
    m["tools"] = len(main_tools)
    answered = [t for t in main_tools if t["error"] is not None]
    m["errors"] = sum(1 for t in answered if t["error"])
    m["flail"] = round(m["errors"] / len(answered), 3) if answered else 0.0

    # Consecutive errored tool calls, in order.
    clusters = []; run = 0
    for t in answered:
        if t["error"]: run += 1
        else:
            if run >= 2: clusters.append(run)
            run = 0
    if run >= 2: clusters.append(run)
    m["error_clusters"] = clusters

    # File churn: >=3 edits to one file inside a 10-tool-call window with no verify between.
    churn = []
    by_file = defaultdict(list)
    for pos, t in enumerate(main_tools):
        if t["kind"] == "edit" and t.get("target"):
            by_file[t["target"]].append(pos)
    verify_pos = [pos for pos, t in enumerate(main_tools) if t["kind"] == "verify"]
    for fp, poss in by_file.items():
        for i in range(len(poss) - 2):
            a, b = poss[i], poss[i + 2]
            if b - a <= 10 and not any(a < v < b for v in verify_pos):
                churn.append((os.path.basename(fp), len(poss)))
                break
    m["churn"] = sorted(set(churn), key=lambda x: -x[1])
    m["edits"] = sum(1 for t in main_tools if t["kind"] == "edit")
    m["verifies"] = len(verify_pos)

    # Identical tool input issued more than once = literal retry.
    sig = Counter(hashlib.md5((t["name"] + json.dumps(t["input"], sort_keys=True,
                  default=str)).encode()).hexdigest() for t in main_tools)
    m["exact_retries"] = sum(v - 1 for v in sig.values() if v > 1)

    # Human rejections of proposed edits: the cleanest rework signal there is,
    # but only some agents record it.
    dec = Counter(str(t.get("decision")).lower() for t in main_tools
                  if t["kind"] == "edit" and t.get("decision"))
    m["rejected"] = sum(v for k, v in dec.items() if "reject" in k or "revert" in k)
    m["decided"] = sum(dec.values())

    # Consecutive human turns. Reported, NOT scored: mid-turn messages look identical.
    cu = 0
    seq = [t["role"] for t in s.turns if not t["sidechain"] and not t["meta"]]
    for a, b in zip(seq, seq[1:]):
        if a == "user" and b == "user": cu += 1
    m["consec_user"] = cu

    m["tool_mix"] = Counter(t["name"] for t in main_tools).most_common(5)
    m["kind_mix"] = Counter(t["kind"] for t in main_tools).most_common()
    m["agent"] = getattr(s, "agent", "claude-code")
    m["tokens"] = dict(s.tokens)
    m["compactions"] = s.compactions
    return m



def archive_prompts(con, now):
    """~/.claude/history.jsonl outlives transcripts by months. Merge, never copy:
    if the source is ever pruned we still hold everything seen before."""
    p = os.path.join(CLAUDE, "history.jsonl")
    if not os.path.exists(p): return (0, 0)
    new = seen = 0
    for line in open(p, errors="replace"):
        try: d = json.loads(line)
        except Exception: continue
        disp = d.get("display") or ""
        ts = d.get("timestamp")
        h = hashlib.sha256(f"{ts}|{d.get('sessionId')}|{disp}".encode()).hexdigest()[:32]
        seen += 1
        iso = None
        if isinstance(ts, (int, float)):
            iso = dt.datetime.fromtimestamp(ts/1000 if ts > 1e11 else ts).astimezone().isoformat()
        cur = con.execute("INSERT OR IGNORE INTO prompts VALUES (?,?,?,?,?,?,?,?)", (
            h, ts, iso, d.get("sessionId"), str(d.get("project") or ""), disp,
            len(d.get("pastedContents") or {}) if isinstance(d.get("pastedContents"), dict) else 0,
            now))
        new += cur.rowcount
    return (new, seen)

def archive_blobs(con, now):
    """file-history blobs are the CONTENT behind trackedFileBackups pointers.
    Content-addressed, so copying is idempotent and never overwrites."""
    root = os.path.join(CLAUDE, "file-history")
    if not os.path.isdir(root): return (0, 0)
    dest_root = os.path.join(ARCHIVE, "_file-history")
    have = {r[0] for r in con.execute("SELECT key FROM blobs")}
    new = total = 0
    for sdir in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(sdir): continue
        sid = os.path.basename(sdir)
        for b in sorted(glob.glob(os.path.join(sdir, "*"))):
            if not os.path.isfile(b): continue
            total += 1
            blob = os.path.basename(b)
            key = f"{sid}/{blob}"
            if key in have: continue
            dd = os.path.join(dest_root, sid)
            os.makedirs(dd, exist_ok=True)
            out = os.path.join(dd, blob)
            shutil.copy2(b, out)
            os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
            con.execute("INSERT OR REPLACE INTO blobs VALUES (?,?,?,?,?,?)",
                        (key, sid, blob, out, os.path.getsize(out), now))
            new += 1
    return (new, total)

def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def cmd_archive(args):
    """Tier 1 always: raw session, gzipped, 0700, local only.
    Nothing leaves this machine. Append-only: a session that grew is re-snapshotted."""
    cfg = privacy.require_consent(ROOT, quiet=getattr(args, "quiet", False))
    if cfg is None: return
    # The SessionEnd hook can fire while a manual run is in progress; a second
    # archiver would just collide on the database.
    lock = os.path.join(ROOT, ".archive.lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode()); os.close(lock_fd)
    except FileExistsError:
        try: pid = int(open(lock).read().strip())
        except Exception: pid = None
        alive = False
        if pid:
            try: os.kill(pid, 0); alive = True
            except OSError: alive = False
        if alive:
            if not getattr(args, "quiet", False):
                print(f"another archive run is in progress (pid {pid}); skipping")
            return
        os.remove(lock)   # stale lock from a killed run
        open(lock, "w").write(str(os.getpid()))
    try:
        _archive_body(args, cfg, con=None)
    finally:
        try: os.remove(lock)
        except OSError: pass
    return

def _archive_body(args, cfg, con=None):
    os.makedirs(ARCHIVE, exist_ok=True)
    os.chmod(ARCHIVE, stat.S_IRWXU)  # 0700, owner only
    os.chmod(ROOT, stat.S_IRWXU)
    redact = cfg.get("redact_on_archive", True) and not getattr(args, "raw", False)
    rcounts = Counter()
    con = sqlite3.connect(DB); ensure_db(con)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    live_pairs = adapters.discover_all()
    live = [f for _, f in live_pairs]
    known = {r[0]: r for r in con.execute(
        "SELECT sid, sha, src_bytes, snapshots, first_seen FROM archive")}
    new = grew = same = 0

    for ad, f in live_pairs:
        sid = os.path.basename(f).split(".")[0].replace("rollout-", "")
        exporter = getattr(ad, "export", None)
        # Claude keeps its project-slug layout; other agents live under the agent name,
        # because their directory trees carry no project identity.
        project = (os.path.basename(os.path.dirname(f))
                   if ad is adapters.ClaudeAdapter else ad.name)
        if exporter:                      # KV-stored session: serialise it first
            try: blob = exporter(f, include_code=cfg.get("cursor_include_code", False))
            except TypeError: blob = exporter(f)
            if not blob: continue
            size = len(blob)
            sha = hashlib.sha256(blob).hexdigest()
        else:
            blob = None
            size = os.path.getsize(f)
            sha = sha_of(f)
        prev = known.get(sid)

        if prev and prev[1] == sha:
            con.execute("UPDATE archive SET last_seen=?, source_present=1 WHERE sid=?", (now, sid))
            same += 1
            continue

        # Shrunk means truncated or rotated, so keep the old copy under a suffix.
        pdir = os.path.join(ARCHIVE, project)
        os.makedirs(pdir, exist_ok=True)
        arc = os.path.join(pdir, sid + ".jsonl.gz")
        snaps = (prev[3] if prev else 0) + 1
        if prev and size < prev[2] and os.path.exists(arc):
            os.rename(arc, os.path.join(pdir, f"{sid}.v{snaps-1}.jsonl.gz"))

        lines = 0
        src_iter = (blob.splitlines(keepends=True) if blob is not None
                    else open(f, "rb"))
        with gzip.open(arc + ".tmp", "wb", compresslevel=6) as fout:
            for raw_line in src_iter:
                lines += 1
                if redact:
                    txt = raw_line.decode("utf-8", "replace")
                    txt = privacy.redact_line(txt, rcounts)
                    fout.write(txt.encode("utf-8"))
                else:
                    fout.write(raw_line)
        if blob is None: src_iter.close()
        os.replace(arc + ".tmp", arc)   # atomic, never a half-written archive
        os.chmod(arc, stat.S_IRUSR | stat.S_IWUSR)

        con.execute("INSERT OR REPLACE INTO archive VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            sid, project, arc, sha, size, os.path.getsize(arc), lines,
            (prev[4] if prev else now), now, 1, snaps))
        if prev: grew += 1
        else: new += 1

    # Sessions we hold that the source no longer has: rescued from cleanup.
    live_sids = {os.path.basename(f).split(".")[0].replace("rollout-", "") for f in live}
    rescued = 0
    for sid, in con.execute("SELECT sid FROM archive WHERE source_present=1").fetchall():
        if sid not in live_sids:
            con.execute("UPDATE archive SET source_present=0 WHERE sid=?", (sid,))
            rescued += 1
    con.commit()

    np_, sp = archive_prompts(con, now)
    nb, tb = archive_blobs(con, now)
    con.commit()

    tot = con.execute("SELECT COUNT(*), SUM(src_bytes), SUM(arc_bytes), SUM(source_present) "
                      "FROM archive").fetchone()
    n, raw, comp, present = tot[0] or 0, tot[1] or 0, tot[2] or 0, tot[3] or 0
    print(f"archived {new} new, {grew} updated, {same} unchanged")
    if rescued: print(f"  !! {rescued} session(s) vanished from source since last run and are held here")
    print(f"  holding {n} sessions ({raw/1e6:.1f}MB raw -> {comp/1e6:.1f}MB gz) in {ARCHIVE}")
    print(f"  {present} still live on disk, {n-present} exist ONLY in this archive")
    if redact:
        got = {k: v for k, v in rcounts.items() if not k.startswith("_")}
        print(f"  redacted before writing: " + (", ".join(f"{k}={v}" for k, v in
              sorted(got.items())) if got else "nothing found"))
        if rcounts.get("_reverted_invalid_json"):
            print(f"    {rcounts['_reverted_invalid_json']} line(s) kept raw "
                  f"(redaction would have broken JSON)")
    else:
        print("  WARNING: --raw, secrets stored verbatim")
    tp = con.execute("SELECT COUNT(*), MIN(iso), MAX(iso) FROM prompts").fetchone()
    print(f"  prompts: +{np_} new of {sp} in history.jsonl -> {tp[0]} held"
          f" ({(tp[1] or '?')[:10]} to {(tp[2] or '?')[:10]})")
    orph = con.execute("SELECT COUNT(DISTINCT sid) FROM prompts WHERE sid NOT IN "
                       "(SELECT sid FROM archive)").fetchone()[0]
    if orph: print(f"    {orph} of those sessions have no transcript anywhere; prompts are all that survives")
    bt = con.execute("SELECT COUNT(*), SUM(bytes) FROM blobs").fetchone()
    print(f"  file snapshots: +{nb} new of {tb} on disk -> {bt[0]} blobs held"
          f" ({(bt[1] or 0)/1e6:.1f}MB of actual file content)")

def cmd_status(args):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM archive ORDER BY last_seen DESC").fetchall()
    except sqlite3.OperationalError:
        print("run `retro archive` first"); return
    if not rows: print("archive empty; run `retro archive`"); return
    print(f"\n{'SESSION':<38}{'PROJECT':<24}{'LINES':>8}{'SIZE':>9}  STATE")
    print("-" * 96)
    for r in rows:
        state = "live" if r["source_present"] else "RESCUED (gone from source)"
        snaps = f' [{r["snapshots"]} snapshots]' if r["snapshots"] > 1 else ""
        print(f'{r["sid"][:36]:<38}{r["project"][-22:]:<24}{r["lines"]:>8}'
              f'{r["arc_bytes"]/1e6:>8.1f}M  {state}{snaps}')
    print()

SESSION_COLS = ('sid,project,title,branch,path,start,\"end\",wall_min,active_min,'
                'human_turns,asst_turns,tools,sub_tools,edits,verifies,errors,flail,'
                'exact_retries,consec_user,compactions,in_tok,out_tok,cache_read,'
                'cache_create,churn,clusters,gaps,tool_mix')
SESSION_INSERT = ('INSERT OR REPLACE INTO sessions (' + SESSION_COLS + ') VALUES ('
                  + ','.join('?' * 28) + ')')

def ensure_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS sessions(
      sid TEXT PRIMARY KEY, project TEXT, title TEXT, branch TEXT, path TEXT,
      start TEXT, end TEXT, wall_min REAL, active_min REAL,
      human_turns INT, asst_turns INT, tools INT, sub_tools INT, edits INT,
      verifies INT, errors INT, flail REAL, exact_retries INT, consec_user INT,
      compactions INT, in_tok INT, out_tok INT, cache_read INT, cache_create INT,
      churn TEXT, clusters TEXT, gaps TEXT, tool_mix TEXT);
    CREATE TABLE IF NOT EXISTS prompts(
      h TEXT PRIMARY KEY, ts INT, iso TEXT, sid TEXT, project TEXT,
      display TEXT, pasted INT, archived_at TEXT);
    CREATE TABLE IF NOT EXISTS blobs(
      key TEXT PRIMARY KEY, sid TEXT, blob TEXT, arc_path TEXT,
      bytes INT, archived_at TEXT);
    CREATE TABLE IF NOT EXISTS archive(
      sid TEXT PRIMARY KEY, project TEXT, arc_path TEXT, sha TEXT,
      src_bytes INT, arc_bytes INT, lines INT, first_seen TEXT, last_seen TEXT,
      source_present INT, snapshots INT);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    if "agent" not in cols:
        con.execute("ALTER TABLE sessions ADD COLUMN agent TEXT DEFAULT 'claude-code'")

def cmd_scan(args):
    by_sid = {}
    for f in sorted(glob.glob(os.path.join(ARCHIVE, "*", "*.jsonl.gz"))):
        b = os.path.basename(f)
        if ".v" in b: continue          # historical snapshot, not the head
        by_sid[b[:-9]] = f
    for a, f in adapters.discover_all():   # live copies are authoritative
        by_sid[os.path.basename(f).split(".")[0].replace("rollout-", "")] = f
    files = [by_sid[k] for k in sorted(by_sid)]
    if not files:
        print(f"no transcripts under {PROJECTS} or {ARCHIVE}"); return
    con = sqlite3.connect(DB); ensure_db(con)
    unknown = Counter(); n = 0
    for f in files:
        s = load(f); m = metrics(s); unknown.update(s.unknown_types)
        tk = m["tokens"]
        con.execute(SESSION_INSERT, (
            s.sid, s.project, s.title, s.branch, f,
            m["start"].isoformat() if m["start"] else None,
            m["end"].isoformat() if m["end"] else None,
            m["wall_min"], m["active_min"], m["human_turns"], m["asst_turns"],
            m["tools"], m["sub_tools"], m["edits"], m["verifies"], m["errors"],
            m["flail"], m["exact_retries"], m["consec_user"], m["compactions"],
            tk.get("input_tokens", 0), tk.get("output_tokens", 0),
            tk.get("cache_read_input_tokens", 0), tk.get("cache_creation_input_tokens", 0),
            json.dumps(m["churn"]), json.dumps(m["error_clusters"]),
            json.dumps(m["idle_gaps"]), json.dumps(m["tool_mix"])))
        con.execute("UPDATE sessions SET agent=? WHERE sid=?", (m["agent"], s.sid))
        n += 1
    con.commit()
    print(f"scanned {n} sessions -> {DB}")
    if unknown: print("unrecognised record types (ignored):", dict(unknown))

def bar(v, hi, w=18):
    if hi <= 0: return ""
    return "#" * max(0, min(w, round(v / hi * w)))

def cmd_friction(args):
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM sessions ORDER BY start").fetchall()
    except sqlite3.OperationalError:
        print("run `retro scan` first"); return
    rows = [r for r in rows if r["human_turns"] or r["tools"]]
    if args.project:
        rows = [r for r in rows if args.project.lower() in (r["project"] or "").lower()]
    if args.agent:
        rows = [r for r in rows if args.agent.lower() in (r["agent"] or "").lower()]
    if not rows: print("no sessions"); return

    print(f"\n{'SESSION':<26}{'AGENT':<13}{'PROJECT':<16}{'MIN':>6}{'TURN':>6}{'TOOL':>6}{'ERR':>5}{'FLAIL':>7}{'EDIT':>6}{'VER':>5}{'RETRY':>6}  CHURN")
    print("-" * 130)
    tot = Counter(); worst = []
    for r in rows:
        churn = json.loads(r["churn"] or "[]")
        cs = ", ".join(f"{f}x{n}" for f, n in churn[:2]) or "-"
        title = (r["title"] or r["sid"][:8])[:24]
        proj = (r["project"] or "")[-20:]
        print(f"{title:<26}{(r['agent'] or '?')[:11]:<13}{proj[-14:]:<16}{r['active_min']:>6.0f}{r['human_turns']:>6}"
              f"{r['tools']:>6}{r['errors']:>5}{r['flail']:>7.2f}{r['edits']:>6}"
              f"{r['verifies']:>5}{r['exact_retries']:>6}  {cs}")
        for k in ("active_min","human_turns","tools","errors","edits","verifies",
                  "exact_retries","consec_user","in_tok","out_tok","cache_read","cache_create"):
            tot[k] += r[k] or 0
        score = (r["errors"] * 2) + r["exact_retries"] + sum(n for _, n in churn)
        worst.append((score, r, churn))

    print("-" * 130)
    ans_flail = tot["errors"] / tot["tools"] if tot["tools"] else 0
    vr = tot["verifies"] / tot["edits"] if tot["edits"] else 0
    print(f"\nTOTALS  {len(rows)} sessions | {tot['active_min']/60:.1f}h active | "
          f"{tot['human_turns']} human turns | {tot['tools']} tool calls")
    print(f"        output {tot['out_tok']/1000:.0f}K tok | input {tot['in_tok']/1000:.0f}K | "
          f"cache-read {tot['cache_read']/1_000_000:.1f}M (replay, not effort)")
    print(f"        flail rate {ans_flail:.1%} of answered tool calls errored")
    print(f"        verify ratio {vr:.2f} verification runs per edit")
    print(f"        exact retries {tot['exact_retries']} identical tool calls re-issued")
    print(f"        consecutive user turns {tot['consec_user']}  (NOISY: mid-turn messages look the same)")

    print("\nHIGHEST-FRICTION SESSIONS")
    hi = max((w[0] for w in worst), default=1)
    for score, r, churn in sorted(worst, key=lambda x: -x[0])[:5]:
        if not score: continue
        print(f"  {bar(score,hi):<19} {(r['title'] or r['sid'][:8])[:40]}")
        bits = []
        if r["errors"]: bits.append(f"{r['errors']} tool errors")
        if r["exact_retries"]: bits.append(f"{r['exact_retries']} exact retries")
        if churn: bits.append("churn: " + ", ".join(f"{f} edited {n}x" for f, n in churn[:3]))
        if r["verifies"] == 0 and r["edits"]: bits.append(f"{r['edits']} edits, ZERO verification runs")
        print(f"{'':21} {' | '.join(bits)}")
        cl = json.loads(r["clusters"] or "[]")
        if cl: print(f"{'':21} error clusters (consecutive failures): {cl}")
    print()


# ---------------------------------------------------------------- sanitizer
JWT = r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
SECRET_RULES = [
 ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
 ("conn_string", re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s\"'<>\\]+")),
 ("jwt",         re.compile(JWT)),
 ("api_key",     re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}"
                            r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{16,}"
                            r"|AIza[A-Za-z0-9_-]{30,}|npm_[A-Za-z0-9]{30,})")),
 ("assigned_secret", re.compile(r"(?i)\b((?:api[_-]?key|secret|passwd|password|token|bearer|auth)"
                                r"\w*\s*[:=]\s*)([\"']?)([A-Za-z0-9_\-\.\/\+]{12,})\2")),
 ("email",       re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
 ("ip",          re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")),
]
SAFE_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "8.8.8.8", "1.1.1.1", "0.0.0.1"}
B64ISH = re.compile(r"\b[A-Za-z0-9+/=_-]{28,}\b")
WORDY = re.compile(r"^[A-Za-z][a-z]+(?:[-_][A-Za-z][a-z]+)+$")

def shannon(s):
    if not s: return 0.0
    from math import log2
    c = Counter(s)
    return -sum((v/len(s)) * log2(v/len(s)) for v in c.values())

class Redactor:
    """Fail closed: anything that looks like a secret is masked, and everything
    masked is reported so a human reviews before the bundle is shared."""
    def __init__(self, strict=False):
        self.counts = Counter(); self.ids = {}; self.strict = strict
        self.uncertain = Counter(); self.files = Counter(); self.hosts = Counter()
        self.user = os.path.basename(os.path.expanduser("~"))

    def _ph(self, kind, value):
        k = (kind, value)
        if k not in self.ids:
            self.ids[k] = sum(1 for kk in self.ids if kk[0] == kind) + 1
        self.counts[kind] += 1
        return f"<{kind.upper()}_{self.ids[k]}>"

    def __call__(self, text):
        if not text: return text
        t = str(text)
        for kind, rx in SECRET_RULES:
            if kind == "assigned_secret":
                def sub(m):
                    if len(m.group(3)) < 12 or WORDY.match(m.group(3)): return m.group(0)
                    return m.group(1) + m.group(2) + self._ph(kind, m.group(3)) + m.group(2)
                t = rx.sub(sub, t)
            elif kind == "ip":
                t = rx.sub(lambda m: m.group(0) if m.group(0) in SAFE_IPS
                           else self._ph(kind, m.group(0)), t)
            else:
                t = rx.sub(lambda m: self._ph(kind, m.group(0)), t)
        # Home directory and username, everywhere they appear.
        t = re.sub(r"/Users/[A-Za-z0-9._-]+", lambda m: (self.counts.__setitem__("home_path",
                   self.counts["home_path"]+1), "/Users/USER")[1], t)
        if self.user:
            t = re.sub(rf"\b{re.escape(self.user)}\b", "USER", t)
        # URLs carry no secret yet leak intent (which job, which client, which repo).
        # Default keeps the host for context; strict drops it too.
        def url(m):
            u = m.group(0)
            try: host = re.match(r"https?://([^/\s]+)", u).group(1)
            except Exception: host = "host"
            self.counts["url"] += 1
            self.hosts[host] += 1
            if self.strict: return self._ph("url", u)
            return f"https://{host}/<PATH_REDACTED>" if "/" in u[8:] or "?" in u else u
        t = re.sub(r"https?://[^\s<>\"')\]]+", url, t)
        # Unrecognised high-entropy blobs: mask, and flag for human review.
        def ent(m):
            s = m.group(0)
            if s.startswith("<") or WORDY.match(s): return s
            if shannon(s) >= 3.9:
                self.uncertain[s[:6] + "..."] += 1
                return self._ph("highentropy", s)
            return s
        t = B64ISH.sub(ent, t)
        return t

    def path(self, p):
        if not p: return p
        base = os.path.basename(p)
        self.files[base] += 1
        if self.strict:
            stem, ext = os.path.splitext(base)
            h = hashlib.sha256(stem.encode()).hexdigest()[:6]
            return f"file_{h}{ext}"
        return base

def find_session(needle):
    cands = {}
    for f in glob.glob(os.path.join(ARCHIVE, "*", "*.jsonl.gz")):
        b = os.path.basename(f)
        if ".v" in b: continue
        cands[b[:-9]] = f
    for a, f in adapters.discover_all():
        cands[os.path.basename(f).split(".")[0].replace("rollout-", "")] = f
    if not needle: return None
    hits = [(k, v) for k, v in cands.items() if needle.lower() in k.lower()]
    if not hits:
        for k, v in cands.items():
            s = load(v)
            if s.title and needle.lower() in s.title.lower(): hits.append((k, v))
    return hits[0][1] if len(hits) >= 1 else None

def cmd_sanitize(args):
    path = find_session(args.session)
    if not path:
        print("no session matched; `retro status` lists them"); return
    s = load(path); m = metrics(s); R = Redactor(strict=args.strict)
    out = os.path.abspath(args.out); os.makedirs(out, exist_ok=True)

    L = []
    L.append(f"# Agent session bundle\n")
    L.append(f"**Task:** {R(s.title or 'untitled')}\n")
    L.append(f"**When:** {str(m['start'])[:10]} | **Active:** {m['active_min']:.0f} min "
             f"| **Turns:** {m['human_turns']} human, {m['asst_turns']} agent\n")
    L.append("\n## Scorecard (deterministic, computed from the transcript)\n")
    L.append("| metric | value |\n|---|---|")
    L.append(f"| tool calls | {m['tools']} |")
    L.append(f"| file edits | {m['edits']} |")
    L.append(f"| verification runs | {m['verifies']} |")
    L.append(f"| verify ratio | {m['verifies']/m['edits']:.2f} |" if m['edits'] else "| verify ratio | n/a |")
    L.append(f"| tool errors | {m['errors']} ({m['flail']:.0%} of answered calls) |")
    L.append(f"| identical calls re-issued | {m['exact_retries']} |")
    L.append(f"| files edited 3+ times without verification | {len(m['churn'])} |")
    L.append(f"| output tokens | {m['tokens'].get('output_tokens',0)/1000:.0f}K |")
    L.append(f"\n**Tool mix:** " + ", ".join(f"{n} x{c}" for n, c in m['tool_mix']) + "\n")

    L.append("\n## Interaction timeline\n")
    tools_by_idx = defaultdict(list)
    for t in s.tools:
        if not t["sidechain"]: tools_by_idx[t["idx"]].append(t)
    n_user = 0
    for turn in s.turns:
        if turn["sidechain"] or turn["meta"]: continue
        if turn["role"] == "user":
            n_user += 1
            L.append(f"\n### Prompt {n_user}\n")
            L.append("> " + (R(turn.get("text") or "").strip().replace("\n", "\n> ") or "_(no text)_"))
        else:
            acts = []
            for t in tools_by_idx.get(turn["idx"], []):
                tgt = ""
                if t["input"].get("file_path"): tgt = " " + R.path(t["input"]["file_path"])
                elif t["input"].get("command"):
                    tgt = " `" + R(str(t["input"]["command"]))[:90] + "`"
                mark = "FAIL" if t["error"] else ("ok" if t["error"] is not None else "-")
                acts.append(f"- `{t['name']}`{tgt} [{mark}]")
            if acts:
                L.append("\n" + "\n".join(acts))

    body = "\n".join(L)
    open(os.path.join(out, "bundle.md"), "w").write(body)

    # Redaction report: what was masked, and what a human must still eyeball.
    rep = ["# Redaction report\n", f"Source session: `{s.sid}`  ",
           f"Mode: {'STRICT (filenames pseudonymised)' if args.strict else 'standard'}\n",
           "\n## Masked\n", "| category | occurrences | distinct |\n|---|---|---|"]
    for k, v in R.counts.most_common():
        d = sum(1 for kk in R.ids if kk[0] == k)
        rep.append(f"| {k} | {v} | {d or '-'} |")
    rep.append("\n## Still exposed: review these before sharing\n")
    rep.append("**Filenames in the bundle** (they can leak project or client names):\n")
    rep.append(", ".join(f"`{f}`" for f, _ in R.files.most_common(40)) or "_none_")
    if R.uncertain:
        rep.append("\n\n**High-entropy strings masked as a precaution** (may be harmless hashes):\n")
        rep.append(", ".join(f"`{k}` x{v}" for k, v in R.uncertain.most_common(20)))
    if R.hosts:
        rep.append("\n\n**Hostnames still visible** (paths and query strings were stripped):\n")
        rep.append(", ".join(f"`{h}` x{c}" for h, c in R.hosts.most_common(20)))
        rep.append("\n\n_A hostname can itself reveal intent (a job board, a client domain). "
                   "Re-run with `--strict` to mask hostnames too._")
    rep.append("\n\n**Not included at all:** source file contents, blob snapshots, "
               "assistant reasoning text, environment variables.\n")
    open(os.path.join(out, "REDACTIONS.md"), "w").write("\n".join(rep))

    resid = re.findall(r"|".join(r[1].pattern for r in SECRET_RULES[:4]), body)
    print(f"\nwrote {out}/bundle.md  ({len(body)} chars)")
    print(f"wrote {out}/REDACTIONS.md")
    print(f"\nmasked: " + (", ".join(f"{k}={v}" for k, v in R.counts.most_common()) or "nothing"))
    print(f"self-check for residual secrets in output: "
          f"{'CLEAN' if not resid else 'FAILED, ' + str(len(resid)) + ' hits'}")
    if R.hosts and not args.strict:
        print(f"hostnames still visible: {len(R.hosts)} -> " + ", ".join(list(R.hosts)[:5]))
    elif R.hosts:
        print(f"hostnames masked: {len(R.hosts)} (listed in REDACTIONS.md for your review)")
    print(f"filenames still visible: {len(R.files)}"
          f"{'  (re-run with --strict to pseudonymise)' if not args.strict else ''}")
    print("\nREVIEW REDACTIONS.md BEFORE SHARING. Nothing was uploaded.\n")

# ---------------------------------------------------------------- hook
def cmd_install_hook(args):
    sp = os.path.join(CLAUDE, "settings.json")
    cfg = {}
    if os.path.exists(sp):
        try: cfg = json.load(open(sp))
        except Exception:
            print(f"{sp} is not valid JSON; refusing to touch it"); return
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        bak = f"{sp}.bak-{stamp}"
        n = 1
        while os.path.exists(bak):      # same-second runs must not clobber each other
            bak = f"{sp}.bak-{stamp}-{n}"; n += 1
        shutil.copy2(sp, bak); print(f"backed up -> {bak}")
    me = os.path.abspath(__file__)
    cmd = f"{sys.executable} {me} archive >/dev/null 2>&1"
    hooks = cfg.setdefault("hooks", {})
    ends = hooks.setdefault("SessionEnd", [])
    existing = [e for e in ends if "retro.py" in json.dumps(e)]
    if args.uninstall:
        hooks["SessionEnd"] = [e for e in ends if "retro.py" not in json.dumps(e)]
        if not hooks["SessionEnd"]: hooks.pop("SessionEnd")
        if not hooks: cfg.pop("hooks")
        json.dump(cfg, open(sp, "w"), indent=2)
        print("retro SessionEnd hook removed"); return
    if existing:
        print("hook already installed; nothing to do"); return
    ends.append({"hooks": [{"type": "command", "command": cmd, "timeout": 60}]})
    json.dump(cfg, open(sp, "w"), indent=2)
    print(f"installed SessionEnd hook in {sp}")
    print(f"  {cmd}")
    print("  every session now checkpoints itself on exit")


def cmd_parity(args):
    """Adapter blind spots differ. Before comparing agents, show what each one
    can actually observe, so a coverage gap is not read as a behaviour difference."""
    rows = []
    for ad, f in adapters.discover_all():
        try: s = ad.load(f)
        except Exception: continue
        rows.append((ad.name, s))
    if not rows: print("no sessions found"); return
    agg = defaultdict(lambda: Counter())
    for name, s in rows:
        a = agg[name]
        a["sessions"] += 1
        real = [t for t in s.turns if not t["meta"]]
        a["turns"] += len(real)
        a["turn_ts"] += sum(1 for t in real if t["ts"])
        # A filled timestamp is not a useful one: carried-forward values collapse
        # to a single instant, so track whether the session spans real time.
        stamps = {t["ts"] for t in real if t["ts"]}
        a["span_ok"] += 1 if len(stamps) > 1 else 0
        a["tools"] += len(s.tools)
        a["tool_ts"] += sum(1 for t in s.tools if t["ts"])
        a["known_kind"] += sum(1 for t in s.tools if t["kind"] != "other")
        a["edit_target"] += sum(1 for t in s.tools if t["kind"] == "edit" and t.get("target"))
        a["edits"] += sum(1 for t in s.tools if t["kind"] == "edit")
        a["err_known"] += sum(1 for t in s.tools if t["error"] is not None)
        a["decided"] += sum(1 for t in s.tools if t["kind"] == "edit" and t.get("decision"))
        a["tok"] += 1 if s.tokens.get("output_tokens") else 0
        a["unknown_rec"] += sum(s.unknown_types.values())

    print(f"\n{'':<14}{'sess':>6}{'turn ts':>9}{'real span':>11}{'kind':>8}{'edit tgt':>10}"
          f"{'err known':>11}{'tokens':>8}{'unk recs':>10}")
    print("-" * 88)
    def pct(a, b): return f"{(a/b*100):.0f}%" if b else "n/a"
    for name, a in sorted(agg.items()):
        print(f"{name:<14}{a['sessions']:>6}{pct(a['turn_ts'],a['turns']):>9}"
              f"{pct(a['span_ok'],a['sessions']):>11}{pct(a['known_kind'],a['tools']):>8}"
              f"{pct(a['edit_target'],a['edits']):>10}{pct(a['err_known'],a['tools']):>11}"
              f"{pct(a['tok'],a['sessions']):>8}{a['unknown_rec']:>10}")
    print("\nEach column is COVERAGE, not behaviour. A metric may only be compared")
    print("across agents where both adapters score high on the columns it depends on:")
    print("  duration      -> real span      churn        -> edit tgt")
    print("  flail rate    -> err known      token spend  -> tokens")
    names = sorted(agg)
    if len(names) >= 2:
        print("\nWARNINGS")
        warned = False
        for col, dep in (("span_ok","duration"),("err_known","flail rate"),
                         ("edit_target","churn"),("tok","token spend")):
            base = ("sessions" if col in ("tok", "span_ok")
                    else ("edits" if col == "edit_target" else "tools"))
            vals = {n: (agg[n][col]/agg[n][base] if agg[n][base] else 0) for n in names}
            lo, hi = min(vals.values()), max(vals.values())
            if hi - lo > 0.25:
                worst = min(vals, key=vals.get)
                print(f"  do NOT compare {dep} across agents: {worst} covers only "
                      f"{lo*100:.0f}% vs {hi*100:.0f}% elsewhere")
                warned = True
        if not warned: print("  none; coverage is comparable across adapters")
    print()


def cmd_consent(args):
    cfg = privacy.load_config(ROOT)
    if args.revoke:
        cfg["consent"] = {"accepted": False, "at": None}
        privacy.save_config(ROOT, cfg)
        print("consent revoked; `retro archive` will not run until re-accepted")
        return
    if args.accept:
        cfg["consent"] = {"accepted": True,
                          "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        privacy.save_config(ROOT, cfg)
        print("consent recorded. Settings:")
        for k in ("redact_on_archive", "cursor_include_code"):
            print(f"  {k}: {cfg.get(k)}")
        print(f"  encryption: {cfg['encryption']['enabled']}  (enable with `retro encrypt`)")
        return
    print(privacy.CONSENT_TEXT.format(root=ROOT))
    print(f"  current: accepted={cfg['consent']['accepted']} at={cfg['consent']['at']}")
    print("  `retro consent --accept` to agree, `--revoke` to withdraw\n")

def cmd_forget(args):
    """Erase archived material. There is no undo."""
    con = sqlite3.connect(DB); ensure_db(con)
    removed_files = removed_rows = 0
    targets = []
    rows = con.execute("SELECT sid, arc_path, project FROM archive").fetchall()
    for sid, arc, project in rows:
        keep = True
        if args.all: keep = False
        if args.session and args.session.lower() in sid.lower(): keep = False
        if args.agent and args.agent.lower() in (project or "").lower(): keep = False
        if args.before:
            r = con.execute("SELECT start FROM sessions WHERE sid=?", (sid,)).fetchone()
            if r and r[0] and r[0][:10] < args.before: keep = False
        if not keep: targets.append((sid, arc))
    if not targets and not args.pattern and not args.all:
        print("nothing matched"); return
    if not args.yes:
        if args.all:
            nf = sum(len(fs) for _, _, fs in os.walk(ARCHIVE))
            print(f"would erase EVERYTHING: {nf} archive file(s), all prompts, all snapshots")
        print(f"would erase {len(targets)} archived session(s):")
        for sid, _ in targets[:10]: print(f"   {sid}")
        if len(targets) > 10: print(f"   ... and {len(targets)-10} more")
        if args.pattern: print(f"would scrub prompts matching /{args.pattern}/")
        print("\nre-run with --yes to actually erase. There is no undo.")
        return
    for sid, arc in targets:
        for p in (arc, arc + ".enc"):
            if p and os.path.exists(p):
                os.remove(p); removed_files += 1
        # blob rows point at real files on disk; delete the bytes, not just the row
        for (bp,) in con.execute("SELECT arc_path FROM blobs WHERE sid=?", (sid,)):
            if bp and os.path.exists(bp):
                os.remove(bp); removed_files += 1
        con.execute("DELETE FROM archive WHERE sid=?", (sid,))
        con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        con.execute("DELETE FROM blobs WHERE sid=?", (sid,))
        con.execute("DELETE FROM prompts WHERE sid=?", (sid,))
        removed_rows += 1
    if args.all:
        # prompts and blobs outlive their transcripts, so --all must sweep them too
        for (bp,) in con.execute("SELECT arc_path FROM blobs"):
            if bp and os.path.exists(bp):
                os.remove(bp); removed_files += 1
        con.execute("DELETE FROM blobs")
        n_pr = con.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        n_se = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        con.execute("DELETE FROM prompts")
        con.execute("DELETE FROM sessions")   # titles and metrics, also derived data
        print(f"  also erased {n_pr} prompt(s), {n_se} session record(s), every file snapshot")
        fh = os.path.join(ARCHIVE, "_file-history")
        if os.path.isdir(fh): shutil.rmtree(fh, ignore_errors=True)
    if args.pattern:
        rx = re.compile(args.pattern, re.I)
        hits = [h for h, d in con.execute("SELECT h, display FROM prompts") if d and rx.search(d)]
        for h in hits: con.execute("DELETE FROM prompts WHERE h=?", (h,))
        print(f"scrubbed {len(hits)} prompt(s) matching /{args.pattern}/")
    con.commit()
    con.execute("VACUUM")
    print(f"erased {removed_rows} session(s), {removed_files} archive file(s)")
    print("note: material already copied elsewhere (backups, bundles) is unaffected")

def cmd_encrypt(args):
    if not privacy.encryption_available():
        print("needs the `cryptography` package: pip install cryptography"); return
    cfg = privacy.load_config(ROOT)
    if cfg["encryption"]["enabled"] and not args.decrypt:
        print("archive is already encrypted"); return
    pw = privacy.get_passphrase()
    if not pw: print("no passphrase given"); return
    if args.decrypt:
        n = privacy.decrypt_archive(ROOT, ARCHIVE, pw)
        print(f"decrypted {n} archive file(s)")
    else:
        n = privacy.encrypt_archive(ROOT, ARCHIVE, pw)
        print(f"encrypted {n} archive file(s)")
        print("keep the passphrase safe: without it the archive is unrecoverable.")
        print("set RETRO_PASSPHRASE for scan/friction to read it back.")


# ------------------------------------------------------------------ heatmap
HEAT_BLOCKS = [" ", "\u2591", "\u2592", "\u2593", "\u2588"]
HEAT_ANSI = ["\033[38;5;236m", "\033[38;5;22m", "\033[38;5;28m",
             "\033[38;5;34m", "\033[38;5;40m"]
RESET = "\033[0m"

def heat_daily(con, metric):
    """Return {date -> value} plus {date -> friction score}."""
    vals = Counter(); fric = Counter()
    if metric == "prompts":
        for (d,) in con.execute("SELECT substr(iso,1,10) FROM prompts WHERE iso IS NOT NULL"):
            if d: vals[d] += 1
    con.row_factory = sqlite3.Row
    for r in con.execute("SELECT * FROM sessions WHERE start IS NOT NULL"):
        d = (r["start"] or "")[:10]
        if not d: continue
        if metric == "sessions": vals[d] += 1
        elif metric == "edits": vals[d] += r["edits"] or 0
        elif metric == "tokens": vals[d] += r["out_tok"] or 0
        elif metric == "tools": vals[d] += r["tools"] or 0
        churn = sum(n for _, n in json.loads(r["churn"] or "[]"))
        fric[d] += (r["errors"] or 0) * 2 + (r["exact_retries"] or 0) + churn
    con.row_factory = None
    return vals, fric

def heat_levels(vals):
    """Quartile buckets, so one huge day cannot flatten the rest."""
    pos = sorted(v for v in vals.values() if v > 0)
    if not pos: return lambda v: 0
    qs = [pos[int(len(pos) * f)] for f in (0.25, 0.5, 0.75)]
    qs = [max(1, q) for q in qs]
    def lvl(v):
        if v <= 0: return 0
        if v <= qs[0]: return 1
        if v <= qs[1]: return 2
        if v <= qs[2]: return 3
        return 4
    return lvl

def heat_grid(vals, days):
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    start -= dt.timedelta(days=start.weekday())        # align to Monday
    cols = []
    d = start
    while d <= end:
        week = []
        for _ in range(7):
            week.append((d, vals.get(d.isoformat(), 0)) if d <= end else (None, 0))
            d += dt.timedelta(days=1)
        cols.append(week)
    return cols, start, end

def cmd_heatmap(args):
    con = sqlite3.connect(DB)
    try:
        vals, fric = heat_daily(con, args.metric)
    except sqlite3.OperationalError:
        print("run `retro scan` first"); return
    if not vals:
        print(f"no data for metric '{args.metric}'; try --metric prompts"); return
    cols, start, end = heat_grid(vals, args.days)
    lvl = heat_levels(vals)
    colour = sys.stdout.isatty() and not args.no_color

    # month ruler
    ruler = [" "] * (len(cols) * 2)
    last = None
    for i, week in enumerate(cols):
        first = next((d for d, _ in week if d), None)
        if first and first.month != last:
            lab = first.strftime("%b")
            if i * 2 + len(lab) <= len(ruler):
                for j, ch in enumerate(lab): ruler[i * 2 + j] = ch
            last = first.month
    print(f"\n     {''.join(ruler)}")
    names = ["Mon", "   ", "Wed", "   ", "Fri", "   ", "Sun"]
    for row in range(7):
        line = []
        for week in cols:
            d, v = week[row]
            if d is None:
                line.append("  "); continue
            L = lvl(v)
            ch = HEAT_BLOCKS[L] * 2
            line.append(f"{HEAT_ANSI[L]}{ch}{RESET}" if colour else ch)
        print(f"{names[row]}  {''.join(line)}")

    active = [d for d, v in vals.items() if v > 0 and start.isoformat() <= d <= end.isoformat()]
    total = sum(v for d, v in vals.items() if start.isoformat() <= d <= end.isoformat())
    # longest run of consecutive active days
    ds = sorted(dt.date.fromisoformat(d) for d in active)
    best = run = 1 if ds else 0
    for a, b in zip(ds, ds[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        best = max(best, run)
    dow = Counter(dt.date.fromisoformat(d).strftime("%a") for d in active)
    order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    busiest = max(((d, v) for d, v in vals.items() if d in active), key=lambda x: x[1],
                  default=("-", 0))

    print(f"\n  metric: {args.metric}  |  window: {start} to {end} ({args.days}d)")
    print(f"  {total:,} total  |  {len(active)} active days  |  longest streak {best}d"
          f"  |  busiest {busiest[0]} ({busiest[1]:,})")
    print("  by weekday: " + "  ".join(f"{k} {dow.get(k,0)}" for k in order))
    quiet = [k for k in order if dow.get(k, 0) == 0]
    if quiet: print(f"  never active on: {', '.join(quiet)}")
    top = [(d, s) for d, s in fric.items() if s and d in active]
    if top:
        top.sort(key=lambda x: -x[1])
        print("  highest-friction days: " +
              ", ".join(f"{d} ({s})" for d, s in top[:3]))
    print("\n  NOTE: volume is not productivity. A dense grid can mean a lot of rework;")
    print("  cross-read it against `retro friction` before drawing conclusions.\n")

    if args.svg:
        heat_svg(cols, lvl, args, start, end, total, len(active), best)
        print(f"  wrote {args.svg}\n")

def heat_svg(cols, lvl, args, start, end, total, active, streak):
    # TOP must clear the title (y=22) and subtitle (y=38) plus the month
    # ruler drawn at TOP-4, or the labels collide.
    CELL, GAP, PAD, TOP = 11, 3, 16, 62
    pal = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]
    w = PAD * 2 + 30 + len(cols) * (CELL + GAP) + 46   # room for the legend
    h = TOP + 7 * (CELL + GAP) + 48
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="monospace" font-size="10">',
         f'<rect width="{w}" height="{h}" rx="8" fill="#0d1117"/>',
         f'<text x="{PAD}" y="22" fill="#c9d1d9" font-size="13">'
         f'{total:,} {args.metric} across {active} active days</text>',
         f'<text x="{PAD}" y="38" fill="#6e7681">{start} to {end} '
         f'&#183; longest streak {streak}d</text>']
    last = None
    for i, week in enumerate(cols):
        first = next((d for d, _ in week if d), None)
        x = PAD + 30 + i * (CELL + GAP)
        if first and first.month != last:
            o.append(f'<text x="{x}" y="{TOP-4}" fill="#6e7681">'
                     f'{first.strftime("%b")}</text>')
            last = first.month
        for r, (d, v) in enumerate(week):
            if d is None: continue
            y = TOP + r * (CELL + GAP)
            o.append(f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2" '
                     f'fill="{pal[lvl(v)]}"><title>{d}: {v:,} {args.metric}</title></rect>')
    for r, nm in ((0, "Mon"), (2, "Wed"), (4, "Fri")):
        o.append(f'<text x="{PAD}" y="{TOP + r*(CELL+GAP) + 9}" fill="#6e7681">{nm}</text>')
    lx = w - PAD - 5 * (CELL + GAP) - 40
    ly = TOP + 7 * (CELL + GAP) + 18
    o.append(f'<text x="{lx-28}" y="{ly+9}" fill="#6e7681">less</text>')
    for i in range(5):
        o.append(f'<rect x="{lx + i*(CELL+GAP)}" y="{ly}" width="{CELL}" height="{CELL}" '
                 f'rx="2" fill="{pal[i]}"/>')
    o.append(f'<text x="{lx + 5*(CELL+GAP) + 4}" y="{ly+9}" fill="#6e7681">more</text>')
    o.append("</svg>")
    open(args.svg, "w").write("\n".join(o))

OVERVIEW = """\
retro  friction analytics and outbound sanitizer for coding-agent transcripts

Reads what Claude Code, Codex and Cursor already write to local disk. Nothing
leaves this machine: there are no network calls anywhere in this tool.

QUICK START
  retro init                 all of the below, in one command

  retro consent --accept     agree once to what gets stored locally
  retro archive              snapshot sessions before the agent deletes them
  retro scan                 parse archive and live sessions into SQLite
  retro friction             the rework scorecard

COMMANDS
    init             first run: consent, archive, scan, then the scorecard

  capture
    consent          show, record or withdraw consent for local archiving
    archive          snapshot every session, stripping credentials on the way in
    status           what is held, and which sessions survive only here
    scan             parse archive and live sessions into SQLite

  read
    friction         rework scorecard: verify ratio, churn, retries, error clusters
    heatmap          activity grid for the last year, optionally exported as SVG
    parity           what each adapter can observe; run before comparing agents

  share
    sanitize         redacted, shareable bundle of one session plus a report

  manage
    install-hook     run `archive` automatically whenever a session ends
    encrypt          encrypt the archive at rest, or --decrypt to reverse it
    forget           erase sessions, prompts and snapshots. There is no undo

Run `retro help <command>` or `retro <command> --help` for options and examples.

ENVIRONMENT
  RETRO_PROJECTS, RETRO_CODEX, RETRO_CURSOR_DB   read copies, not the live stores
  RETRO_CLAUDE                                   ~/.claude, for prompts and blobs
  RETRO_HOME                                     where retro keeps its own data
  RETRO_ARCHIVE, RETRO_DB                        relocate those individually
  RETRO_PASSPHRASE                               read back an encrypted archive

Metrics are deterministic. No model is involved and none is required.
"""

RAW = argparse.RawDescriptionHelpFormatter


def cmd_init(args):
    """Nothing to a scorecard in one command.

    Time to first insight decides whether a stranger ever sees a number, so
    consent, archive, scan and friction happen here in sequence rather than as
    four things to discover.
    """
    cfg = privacy.load_config(ROOT)
    if not cfg["consent"]["accepted"]:
        print(privacy.CONSENT_TEXT.format(root=ROOT))
        if args.yes:
            agreed = True
        elif not sys.stdin.isatty():
            print("  Not a terminal. Re-run with --yes to accept.\n")
            return
        else:
            try:
                agreed = input("  Proceed? [y/N] ").strip().lower() in ("y", "yes")
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if not agreed:
            print("  Nothing archived.\n")
            return
        cfg["consent"] = {"accepted": True,
                          "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        privacy.save_config(ROOT, cfg)
        print("  consent recorded\n")

    ns = argparse.Namespace
    print("[1/3] archiving sessions before your agent deletes them")
    cmd_archive(ns(raw=False, quiet=True))
    print("\n[2/3] parsing")
    cmd_scan(ns())
    print("\n[3/3] scorecard")
    cmd_friction(ns(project=None, agent=None))
    print("  Next:")
    print("    retro install-hook   archive automatically when a session ends")
    print("    retro heatmap        a year of activity at a glance")
    print("    retro help           everything else\n")


def build_parser():
    """Returns the parser plus a name -> subparser map, so `retro help <cmd>`
    can print one command's help without re-deriving it."""
    p = argparse.ArgumentParser(
        prog="retro", usage="retro <command> [options]",
        description=OVERVIEW, formatter_class=RAW)
    # No `required=True`: a bare `retro` should print help, not an argparse error.
    # help=SUPPRESS: the COMMANDS section above is hand-written, so argparse
    # must not also print its own bare list of names.
    sub = p.add_subparsers(dest="cmd", metavar="<command>",
                           help=argparse.SUPPRESS)
    subs = {}

    def add(name, desc, epilog=None):
        # Explicit prog: otherwise argparse derives it from the parent's usage
        # string and error messages read "retro <command> [options] sanitize:".
        sp = sub.add_parser(name, prog=f"retro {name}", description=desc,
                            epilog=epilog, formatter_class=RAW,
                            usage=f"retro {name} [options]")
        subs[name] = sp
        return sp

    it = add("init",
             "First run, in one command: record consent, archive every session,\n"
             "parse it, and print the scorecard.\n\n"
             "Equivalent to `consent --accept`, `archive`, `scan`, `friction`.",
             "Examples:\n"
             "  retro init          prompts before storing anything\n"
             "  retro init --yes    accept without prompting, for scripts")
    it.add_argument("--yes", action="store_true",
                    help="accept the consent notice without prompting")
    it.set_defaults(fn=cmd_init)

    cs = add("consent",
             "Show what archiving stores on this machine, and record or withdraw\n"
             "agreement. `retro archive` refuses to run until consent is recorded.",
             "Examples:\n"
             "  retro consent            print what would be stored, and current state\n"
             "  retro consent --accept   agree, and enable archiving\n"
             "  retro consent --revoke   withdraw; archiving stops until re-accepted")
    cs.add_argument("--accept", action="store_true", help="record agreement")
    cs.add_argument("--revoke", action="store_true", help="withdraw agreement")
    cs.set_defaults(fn=cmd_consent)

    ar = add("archive",
             "Snapshot every session from every supported agent into the local\n"
             "archive, gzipped and owner-readable only.\n\n"
             "Credentials are redacted BEFORE anything is written, and each edited\n"
             "line is re-parsed to confirm it is still valid JSON. Cursor's verbatim\n"
             "before/after source code is excluded unless you enable it in config.\n"
             "Re-running is cheap: unchanged sessions are skipped by content hash.",
             "Examples:\n"
             "  retro archive            snapshot anything new or changed\n"
             "  retro archive --quiet    for hooks and cron; suppresses the consent notice")
    ar.add_argument("--raw", action="store_true",
                    help="store secrets verbatim, skipping redaction (not advised)")
    ar.add_argument("--quiet", action="store_true",
                    help="suppress the consent notice when run non-interactively")
    ar.set_defaults(fn=cmd_archive)

    st = add("status",
             "List every archived session: line count, size on disk, and whether it\n"
             "still exists in the agent's own store or now survives only here.",
             "Sessions marked RESCUED were deleted by the agent's retention cleanup\n"
             "and exist nowhere else.")
    st.set_defaults(fn=cmd_status)

    sc = add("scan",
             "Parse the archive plus any live sessions into SQLite, computing every\n"
             "metric. Live copies win over archived ones. Safe to re-run.",
             "Unrecognised record types are counted and reported rather than\n"
             "crashing, because these transcript formats change without notice.")
    sc.set_defaults(fn=cmd_scan)

    fr = add("friction",
             "The rework scorecard. Per session: active minutes, human turns, tool\n"
             "calls, errors, edits, verification runs, byte-identical retries, and\n"
             "which files were edited repeatedly with no verification in between.\n\n"
             "Then the highest-friction sessions, ranked by errors, retries and churn.",
             "Examples:\n"
             "  retro friction                      every session\n"
             "  retro friction --agent cursor        one agent only\n"
             "  retro friction --project acme-api    one project only\n\n"
             "The headline number is verification runs per edit. Token volume is not\n"
             "reported as effort: cached context replay dominates it.")
    fr.add_argument("--project", default=None, help="only sessions whose project matches")
    fr.add_argument("--agent", default=None,
                    help="only one agent: claude-code, codex or cursor")
    fr.set_defaults(fn=cmd_friction)

    hm = add("heatmap",
             "A contribution-style activity grid for the last year, plus streaks,\n"
             "weekday distribution and the highest-friction days.",
             "Examples:\n"
             "  retro heatmap                          prompts, last 365 days\n"
             "  retro heatmap --metric edits --days 90\n"
             "  retro heatmap --svg ~/Desktop/mine.svg  write it outside the repo\n\n"
             "Levels are quartiles of your own active days, not absolute counts, so\n"
             "one enormous day cannot flatten the rest of the grid.\n\n"
             "Caveat: `prompts` reads Claude Code's history file only. The other\n"
             "metrics need a session timestamp, and Cursor supplies one for a small\n"
             "fraction of sessions. Check `retro parity` before reading too much in.")
    hm.add_argument("--metric", default="prompts",
                    choices=["prompts", "sessions", "edits", "tools", "tokens"],
                    help="what each cell counts (default: prompts)")
    hm.add_argument("--days", type=int, default=365,
                    help="window size in days (default: 365)")
    hm.add_argument("--svg", default=None,
                    help="also write a standalone SVG with per-day tooltips")
    hm.add_argument("--no-color", action="store_true",
                    help="plain block characters instead of ANSI colour")
    hm.set_defaults(fn=cmd_heatmap)

    pa = add("parity",
             "What each adapter can actually observe: timestamp coverage, whether\n"
             "sessions span real time, tool classification, edit targets, error\n"
             "status, token counts, and unrecognised records.",
             "Run this before quoting any cross-agent number. Adapters are blind in\n"
             "different places, and a coverage gap looks exactly like a behavioural\n"
             "difference. Any metric whose inputs are not covered on both sides is\n"
             "reported as not comparable.")
    pa.set_defaults(fn=cmd_parity)

    sa = add("sanitize",
             "Produce a shareable bundle for one session: a scorecard, a turn-by-turn\n"
             "timeline with every tool call marked ok or FAIL, and a redaction report.\n\n"
             "Fails closed. Anything resembling a secret is masked, and everything\n"
             "masked is listed for you to read before you send it anywhere.",
             "Examples:\n"
             "  retro sanitize kafka --out ./bundle           match title or session id\n"
             "  retro sanitize kafka --out ./bundle --strict   also mask filenames, hosts\n\n"
             "Read REDACTIONS.md before sharing. Regex catches secrets, paths and\n"
             "addresses; it does not catch context that has no shape, such as a URL\n"
             "that reveals what you were working on.")
    sa.add_argument("session", help="session id, or a substring of its title")
    sa.add_argument("--out", default="./bundle", help="output directory (default: ./bundle)")
    sa.add_argument("--strict", action="store_true",
                    help="also pseudonymise filenames and mask hostnames")
    sa.set_defaults(fn=cmd_sanitize)

    ih = add("install-hook",
             "Add a SessionEnd hook to ~/.claude/settings.json so `archive` runs\n"
             "whenever a session closes. Backs the file up first and merges rather\n"
             "than overwriting. Archiving takes a PID lock, so the hook cannot\n"
             "collide with a manual run.",
             "Examples:\n"
             "  retro install-hook             install (idempotent)\n"
             "  retro install-hook --uninstall remove it again")
    ih.add_argument("--uninstall", action="store_true", help="remove the hook")
    ih.set_defaults(fn=cmd_install_hook)

    en = add("encrypt",
             "Encrypt every archive file at rest with scrypt and Fernet. Requires\n"
             "the `cryptography` package; nothing else in retro needs it.",
             "Examples:\n"
             "  retro encrypt                          prompts for a passphrase\n"
             "  RETRO_PASSPHRASE=... retro scan         read an encrypted archive back\n"
             "  retro encrypt --decrypt                restore plaintext\n\n"
             "Lose the passphrase and the archive is unrecoverable. This is\n"
             "all-or-nothing: there is no per-session encryption and no key rotation.")
    en.add_argument("--decrypt", action="store_true", help="reverse encryption")
    en.set_defaults(fn=cmd_encrypt)

    fg = add("forget",
             "Erase archived material. Dry run unless --yes is given.\n\n"
             "Removes archive files, file-snapshot blobs, prompt rows and the derived\n"
             "metrics. It cannot reach copies that already left: backups, bundles you\n"
             "generated, or anything you already shared.",
             "Examples:\n"
             "  retro forget --before 2026-01-01        dry run\n"
             "  retro forget --before 2026-01-01 --yes  actually erase\n"
             "  retro forget --session 06c5c27b --yes   one session\n"
             "  retro forget --agent cursor --yes       everything from one agent\n"
             "  retro forget --pattern 'api key' --yes  scrub matching prompts\n"
             "  retro forget --all --yes                everything. No undo.")
    fg.add_argument("--session", help="session id, or a substring of one")
    fg.add_argument("--agent", help="match the agent or project label")
    fg.add_argument("--before", help="sessions starting before this date (YYYY-MM-DD)")
    fg.add_argument("--pattern", help="regex; matching prompts are scrubbed")
    fg.add_argument("--all", action="store_true", help="erase everything retro holds")
    fg.add_argument("--yes", action="store_true", help="required to actually delete")
    fg.set_defaults(fn=cmd_forget)

    return p, subs


# The things people actually type when they want help.
HELP_WORDS = ("help", "-help", "--help", "-h", "-?", "--?", "?")


def main():
    p, subs = build_parser()
    argv = sys.argv[1:]

    if not argv:
        p.print_help()
        sys.exit(1)

    # `retro help`, `retro -help`, `retro help friction`, `retro ? friction`
    if argv[0] in HELP_WORDS:
        target = argv[1] if len(argv) > 1 else None
        if target in subs:
            subs[target].print_help()
        elif target:
            print(f"retro: no such command {target!r}\n")
            p.print_help()
            sys.exit(1)
        else:
            p.print_help()
        return

    # `retro friction help` reads as a request for help, not a bad argument.
    if len(argv) > 1 and argv[0] in subs and argv[1] in HELP_WORDS:
        subs[argv[0]].print_help()
        return

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        sys.exit(1)
    a.fn(a)

if __name__ == "__main__":
    main()
