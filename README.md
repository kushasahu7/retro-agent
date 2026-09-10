# retro

Local-only analytics over your coding-agent transcripts. Reads what Claude Code,
Codex and Cursor already write to disk, and answers questions their own dashboards
do not: **where did you have to say it twice**, and **can this session be shared
with anyone else**.

Nothing leaves your machine. No network calls, no API keys, no model required.

```bash
retro archive    # checkpoint every session before the agent deletes it
retro scan       # parse archive + live sessions into SQLite
retro friction   # rework scorecard
retro parity     # what each adapter can and cannot see
retro sanitize   # redacted, shareable bundle of one session
retro status     # what is held, and what survives only here
```

## Why

Three separate problems, one parser.

**1. Your history is being deleted.** Claude Code removes session transcripts after
30 days by default (`cleanupPeriodDays`), silently and with no recovery path. If you
have never changed that setting, last month is already gone. `retro archive` snapshots
sessions before the window closes, and reports which ones now exist only in the archive.

**2. Token counts do not measure work.** A high-volume session is equally consistent
with productive work and with extensive rework. In one real corpus, cached-context
reads outnumbered generated tokens **296 to 1**, so anything ranking on "tokens used"
is ranking on context replay. retro computes rework directly from transcript structure
instead: file churn, verification ratio, repeated identical calls, error clusters.

**3. Transcripts cannot be shared.** Interviews increasingly ask for agent transcripts,
and nobody can hand one over. A single 26-session corpus contained 1,336 home-directory
paths, 360 email addresses, 68 IP addresses, plus API-key-shaped strings and database
connection URLs. Every existing redaction tool for these agents is *inbound* (stopping
secrets reaching the model). `retro sanitize` is outbound.

## Metrics

All deterministic. No LLM judges anything.

| metric | meaning |
|---|---|
| **verify ratio** | verification runs per file edit. The headline number. |
| **churn** | a file edited 3+ times inside a 10-call window with no verification between |
| **exact retries** | byte-identical tool calls re-issued |
| **flail rate** | share of answered tool calls that errored |
| **error clusters** | runs of consecutive failures |
| **active time** | wall time minus gaps over 5 minutes |

## Supported agents

| | store | retention | notes |
|---|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | ~30 days | named tools; richest error signal |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | long | shell-only tools; two format generations |
| Cursor | `state.vscdb` KV (`composerData:`, `bubbleId:`) | long | named tools; records accept/reject per edit |

Adapters normalise everything into one IR whose load-bearing field is `kind`
(`edit`/`read`/`shell`/`verify`/`search`/`web`/`other`), not the tool's name. Claude Code
names its tools; Codex routes nearly everything through `shell`. Metrics never see an
agent-specific string.

## Adapter parity: read this before comparing agents

Adapters have different blind spots, and **a coverage gap looks exactly like a
behavioural difference**. `retro parity` reports what each adapter can observe and
refuses to let you compare a metric whose inputs are not covered on both sides.

```
                sess  turn ts  real span    kind  edit tgt  err known  tokens
claude-code        8     100%       100%     75%       99%       100%    100%
codex              9     100%        44%     91%      100%       100%     44%
cursor           145      28%         9%     95%      100%       100%      0%

WARNINGS
  do NOT compare duration across agents: cursor covers only 9% vs 100% elsewhere
  do NOT compare token spend across agents: cursor covers only 0% vs 100% elsewhere
```

Run it before quoting any cross-agent number.

## Privacy model

- Everything is local. No network calls anywhere in the codebase.
- `archive/` is `0700`, archived files `0600`, the SQLite DB `0600`.
- `sanitize` fails closed: anything that looks like a secret is masked, and everything
  masked is listed in `REDACTIONS.md` for human review before sharing.
- Cursor's store is opened **read-only** (`?mode=ro`) so a live editor is never at risk.

**The archive is a permanent copy of data that was previously ephemeral.** That is the
point of the tool and also its main risk. See Gaps.

## Gaps and limitations

Honest list. Several of these are load-bearing.

### Security and privacy

- **The archive is unencrypted at rest.** `0700` protects against other local users,
  not a stolen laptop, and not a backup tool (Time Machine, Dropbox, iCloud) sweeping
  the directory into cloud storage. Encryption is not implemented.
- **Archiving makes secrets permanent.** Credentials that would have expired with the
  30-day cleanup now persist indefinitely. There is no secret-scanning-at-archive-time,
  no retention policy on the archive, and no `retro forget`.
- **The archive stores third-party personal data.** Transcripts contain other people's
  email addresses and correspondence. If this is ever more than personal use, that is a
  GDPR/CCPA question the tool does not currently help with.
- **Sanitized bundles may still carry employer IP.** Sharing one could breach an NDA or
  employment agreement. The tool cannot evaluate that for you.

### Sanitizer

- **No semantic redaction pass.** Regex catches secrets, paths, emails and IPs. It does
  not catch *contextual* leaks that have no shape. A real example: a fully "clean" bundle
  still contained a job-board URL with posting IDs, revealing that the author was job
  hunting. Hostnames are visible by default (`--strict` masks them). A local-model pass
  is the intended fix and is not built.
- **File snapshot content is excluded entirely**, so bundles contain no before/after
  diffs. That is safe but removes the strongest evidence for a portfolio use case.
- **Filenames are visible by default.** `--strict` pseudonymises them.

### Metrics

- **`exact retries` overcounts.** Legitimately repeated idempotent commands
  (`git status`, re-reading a file) count as retries. Needs an idempotent-command
  whitelist.
- **`consecutive user turns` is noise** and is reported but deliberately not scored.
  Mid-turn messages are indistinguishable from correcting a drifting agent.
- **Edit detection for shell-driven agents is heuristic.** Codex edits are shell
  invocations, so detection relies on parsing `apply_patch`, `sed -i`, `tee` and
  redirects. Redirect targets must look like real files, which will miss some edits
  and may still catch some non-edits.
- **Verification detection is a regex over shell commands.** Verification performed
  through an IDE, a test runner UI, or a watcher is invisible. A low verify ratio may
  mean adapter blindness rather than user behaviour.

### Per-agent

- **Codex:** older sessions carry no per-record timestamps, so durations report `0`
  rather than being estimated. `custom_tool_call_output` has no exit code, so error
  detection there is text-heuristic and weaker than Claude Code's `is_error`.
  `patch_apply_end` can double count against shell `apply_patch` in mixed-format sessions.
- **Cursor:** no token counts at all (0% coverage). Most bubbles have no usable
  timestamp (9% of sessions span real time). The `userDecision` accept/reject field is
  captured but **not yet validated**: an observed 99.6% accept rate more likely reflects
  auto-apply behaviour than deliberate per-edit approval. Do not quote it as a quality
  metric. Cursor's KV schema is undocumented and shifts between versions.
- **Claude Code:** `kind` coverage is 75% because MCP tools are classified `other`.
- The archiver does not yet cover `tasks/`, `shell-snapshots/` or `backups/`, which the
  same cleanup also removes.

### Engineering

- No tests. None.
- The `install-hook` idempotency guard and `--uninstall` path are written but **untested**.
- Format drift is guaranteed. Unrecognised record types are counted and reported rather
  than crashing, and that counter has already caught real bugs, but new record types are
  silently excluded from metrics until an adapter is updated.
- Single-user, single-machine. No sync, no team view.
- `scan` re-parses everything each run; there is no incremental mode.

## Install

Python 3.9+, standard library only.

```bash
git clone <this repo> ~/retro-agent
cd ~/retro-agent
python3 retro.py archive
python3 retro.py scan
python3 retro.py friction
```

Run the archiver automatically on every session exit:

```bash
python3 retro.py install-hook      # adds a SessionEnd hook to ~/.claude/settings.json
```

It backs up `settings.json` first and merges rather than overwriting.

## Status

Working prototype, built and validated against a real 162-session corpus across three
agents. Not packaged, not tested, not hardened. Read Gaps before trusting a number.

## License

MIT
