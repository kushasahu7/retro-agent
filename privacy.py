"""Privacy controls for the archive.

The archive turns data that agents delete after 30 days into data that lives
forever. That is the point of the tool and also its main hazard, so the store
is consented to, redacted on the way in, optionally encrypted, and erasable.
"""
import json, os, re, sys, stat, base64, hashlib, gzip, shutil
import datetime as dt

CONFIG_VERSION = 1

def config_path(root):
    return os.path.join(root, "config.json")

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "consent": {"accepted": False, "at": None},
    "redact_on_archive": True,     # strip credentials before they are stored
    "cursor_include_code": False,  # Cursor stores full before/after source
    "encryption": {"enabled": False, "salt": None},
}

def load_config(root):
    p = config_path(root)
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(p):
        try:
            cfg.update(json.load(open(p)))
        except Exception:
            pass
    return cfg

def save_config(root, cfg):
    os.makedirs(root, exist_ok=True)
    p = config_path(root)
    with open(p, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)

CONSENT_TEXT = """
retro is about to create a PERMANENT local copy of data your coding agents
currently delete on a schedule.

  Stored:   full session transcripts, your prompt history, and file snapshots
  Location: {root}
  Contains: whatever your sessions contained, which in practice means
            credentials, database URLs, other people's email addresses,
            and proprietary source code

  Protections applied by default:
    - directory 0700, files 0600, database 0600
    - credentials redacted BEFORE they are written  (redact_on_archive)
    - Cursor before/after source code excluded      (cursor_include_code=false)
    - no network access anywhere in this tool

  Still your responsibility:
    - the archive is NOT encrypted unless you run `retro encrypt`
    - exclude {root} from Time Machine / iCloud / Dropbox, or your
      credential history ends up in cloud backups
    - the archive holds third-party personal data; if this is not purely
      personal use, that carries legal obligations

  You can erase anything later with `retro forget`.
"""

def require_consent(root, quiet=False):
    cfg = load_config(root)
    if cfg["consent"]["accepted"]:
        return cfg
    if not quiet:
        print(CONSENT_TEXT.format(root=root))
        print("  Not archiving. Run `retro consent --accept` to proceed.\n")
    return None

# ------------------------------------------------------------------ redaction
SECRET_RULES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("conn_string", re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s\"'<>\\]+")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    ("api_key", re.compile(r"(?:sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}"
                           r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{16,}"
                           r"|AIza[A-Za-z0-9_-]{30,}|npm_[A-Za-z0-9]{30,})")),
    ("assigned_secret", re.compile(r"(?i)((?:api[_-]?key|secret|passwd|password|token|bearer|auth)\w*"
                                   r"\\?[\"']?\s*[:=]\s*\\?[\"']?)([A-Za-z0-9_\-\.\/\+]{16,})")),
]

def redact_line(line, counts):
    """Redact one JSONL line, keeping it valid JSON. Falls back to the original
    line if a substitution would corrupt the record."""
    out = line
    for kind, rx in SECRET_RULES:
        if kind == "assigned_secret":
            def sub(m):
                counts[kind] += 1
                return m.group(1) + f"<REDACTED_{kind.upper()}>"
            out = rx.sub(sub, out)
        else:
            def sub2(m, k=kind):
                counts[k] += 1
                return f"<REDACTED_{k.upper()}>"
            out = rx.sub(sub2, out)
    if out == line:
        return line
    try:
        json.loads(out)
        return out
    except Exception:
        counts["_reverted_invalid_json"] += 1
        return line

CURSOR_CODE_FIELDS = ("oldString", "newString", "contents", "code_edit",
                      "old_string", "new_string", "codeBlocks", "diffHistories")

def strip_cursor_code(obj):
    """Remove verbatim source code from a Cursor bubble, keeping the metadata
    that metrics need (tool name, target path, status, decision)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in CURSOR_CODE_FIELDS:
                out[k] = f"<CODE_OMITTED:{len(str(v))}b>"
            else:
                out[k] = strip_cursor_code(v)
        return out
    if isinstance(obj, list):
        return [strip_cursor_code(x) for x in obj]
    return obj

# ------------------------------------------------------------------ encryption
def _fernet(passphrase, salt):
    from cryptography.fernet import Fernet
    # OpenSSL caps scrypt memory at 32MB by default; n=2**15,r=8 needs more
    key = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1,
                         dklen=32, maxmem=96 * 1024 * 1024)
    return Fernet(base64.urlsafe_b64encode(key))

def encryption_available():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False

def get_passphrase(prompt="passphrase: "):
    p = os.environ.get("RETRO_PASSPHRASE")
    if p: return p
    # getpass blocks forever when there is no terminal (hooks, cron, pipes).
    if not sys.stdin.isatty():
        return None
    import getpass
    try: return getpass.getpass(prompt)
    except Exception: return None

def encrypt_archive(root, archive, passphrase):
    cfg = load_config(root)
    salt = base64.b64decode(cfg["encryption"]["salt"]) if cfg["encryption"]["salt"] \
        else os.urandom(16)
    f = _fernet(passphrase, salt)
    n = 0
    for dirpath, _, files in os.walk(archive):
        for fn in files:
            if not fn.endswith(".gz") or fn.endswith(".enc"): continue
            p = os.path.join(dirpath, fn)
            data = open(p, "rb").read()
            open(p + ".enc", "wb").write(f.encrypt(data))
            os.chmod(p + ".enc", stat.S_IRUSR | stat.S_IWUSR)
            os.remove(p)
            n += 1
    cfg["encryption"] = {"enabled": True, "salt": base64.b64encode(salt).decode()}
    save_config(root, cfg)
    return n

def decrypt_archive(root, archive, passphrase):
    cfg = load_config(root)
    if not cfg["encryption"]["salt"]: return 0
    f = _fernet(passphrase, base64.b64decode(cfg["encryption"]["salt"]))
    n = 0
    for dirpath, _, files in os.walk(archive):
        for fn in files:
            if not fn.endswith(".enc"): continue
            p = os.path.join(dirpath, fn)
            open(p[:-4], "wb").write(f.decrypt(open(p, "rb").read()))
            os.chmod(p[:-4], stat.S_IRUSR | stat.S_IWUSR)
            os.remove(p)
            n += 1
    cfg["encryption"]["enabled"] = False
    save_config(root, cfg)
    return n

def open_maybe_encrypted(path, root):
    """Return decompressed text for a .gz or .gz.enc archive member."""
    if path.endswith(".enc"):
        cfg = load_config(root)
        pw = get_passphrase()
        if not pw or not cfg["encryption"]["salt"]:
            raise RuntimeError("archive is encrypted; set RETRO_PASSPHRASE")
        f = _fernet(pw, base64.b64decode(cfg["encryption"]["salt"]))
        import io
        raw = f.decrypt(open(path, "rb").read())
        return io.TextIOWrapper(io.BytesIO(gzip.decompress(raw)), errors="replace")
    return gzip.open(path, "rt", errors="replace")
