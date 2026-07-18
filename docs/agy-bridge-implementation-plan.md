# Agy Bridge — Detailed Implementation Plan
## Incremental Build-Up · One Function Per Step

**Based on:** `docs/agy-bridge-safety-architecture.md`
**Repo:** `hah23255/agy-to-im`
**Principle:** Each step delivers exactly one function or activity. Steps are sequential — each builds on the previous. No step depends on a later step.

---

## Phase 0: Plumbing (4 steps)

### Step 0.1 — Add `SafetyConfig` dataclasses

**File:** `src/config.py`
**What:** Define the configuration data structures that all subsequent steps will read from.

```python
from dataclasses import dataclass, field

@dataclass
class CategoryConfig:
    mime_types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    max_size_bytes: int = 52_428_800
    routing: str = "block"

@dataclass
class MediaSafetyConfig:
    max_photo_bytes: int = 20_971_520
    max_file_bytes: int = 52_428_800
    categories: dict[str, CategoryConfig] = field(default_factory=dict)
    default_routing: str = "block"

@dataclass
class QueueConfig:
    max_depth: int = 10
    max_per_user: int = 5
    cooldown_seconds: int = 2

@dataclass  
class MemoryConfig:
    limit_bytes: int = 805_306_368
    check_interval_loops: int = 30

@dataclass
class InboxConfig:
    max_age_hours: int = 24
    max_total_bytes: int = 524_288_000

@dataclass
class SafetyConfig:
    media: MediaSafetyConfig = field(default_factory=MediaSafetyConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)
```

**Verify:** `python -c "from src.config import SafetyConfig; c=SafetyConfig(); print(c.media.default_routing)"` → prints `block`

---

### Step 0.2 — Add `SafetyConfig` to Config

**File:** `src/config.py`
**What:** Attach the safety config to the main `Config` dataclass so it's available everywhere.

```python
# In Config dataclass, add field:
safety: SafetyConfig = field(default_factory=SafetyConfig)
```

**Verify:** `python -c "from src.config import Config; c=Config(telegram=..., agy=...); print(c.safety.queue.max_depth)"` → prints `10`

---

### Step 0.3 — Parse `safety` section from `config.json`

**File:** `src/config.py`
**What:** Extend `load_config()` to read the optional `safety` block from JSON.

```python
def _parse_safety(raw: dict | None) -> SafetyConfig:
    if not raw:
        return SafetyConfig()
    # Parse media categories
    categories: dict[str, CategoryConfig] = {}
    for cat_name, cat_raw in (raw.get("media", {}).get("categories", {}) or {}).items():
        categories[cat_name] = CategoryConfig(
            mime_types=list(cat_raw.get("mime_types", [])),
            extensions=list(cat_raw.get("extensions", [])),
            max_size_bytes=int(cat_raw.get("max_size_bytes", 52_428_800)),
            routing=str(cat_raw.get("routing", "block")),
        )
    media = MediaSafetyConfig(
        max_photo_bytes=int(raw.get("media", {}).get("max_photo_bytes", 20_971_520)),
        max_file_bytes=int(raw.get("media", {}).get("max_file_bytes", 52_428_800)),
        categories=categories,
        default_routing=str(raw.get("media", {}).get("default_routing", "block")),
    )
    queue = QueueConfig(
        max_depth=int(raw.get("queue", {}).get("max_depth", 10)),
        max_per_user=int(raw.get("queue", {}).get("max_per_user", 5)),
        cooldown_seconds=int(raw.get("queue", {}).get("cooldown_seconds", 2)),
    )
    memory = MemoryConfig(
        limit_bytes=int(raw.get("memory", {}).get("limit_bytes", 805_306_368)),
        check_interval_loops=int(raw.get("memory", {}).get("check_interval_loops", 30)),
    )
    inbox = InboxConfig(
        max_age_hours=int(raw.get("inbox", {}).get("max_age_hours", 24)),
        max_total_bytes=int(raw.get("inbox", {}).get("max_total_bytes", 524_288_000)),
    )
    return SafetyConfig(media=media, queue=queue, memory=memory, inbox=inbox)
```

Wire into `load_config()`:
```python
safety = _parse_safety(raw.get("safety"))
return Config(telegram=..., agy=..., safety=safety)
```

**Verify:** Create a minimal `config.json` with `"safety": {"queue": {"max_depth": 3}}`, load it, check `cfg.safety.queue.max_depth == 3`.

---

### Step 0.4 — Write default `safety` section to `config.example.json`

**File:** `config.example.json`
**What:** Provide a documented reference config with all safety options visible.

Copy the full `safety` schema from the architecture document's Section 1.2.

**Verify:** `python -c "from src.config import load_config; c=load_config('config.example.json'); print(len(c.safety.media.categories))"` → prints the category count.

---

## Phase 1: File Classification (4 steps)

### Step 1.1 — Wildcard MIME matcher

**File:** `src/media.py`
**What:** Match MIME types including wildcards (`text/*`, `application/vnd.*`).

```python
import fnmatch

def _mime_matches(actual: str, pattern: str) -> bool:
    """Match MIME types with glob-style wildcards.
    'text/plain' matches 'text/*'.
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document' matches 'application/vnd.*'.
    """
    if pattern == actual:
        return True
    if pattern.endswith("/*"):
        prefix = pattern[:-1]
        return actual.startswith(prefix)
    if "*" in pattern:
        return fnmatch.fnmatch(actual, pattern)
    return False
```

**Verify:**
```python
assert _mime_matches("text/plain", "text/*") == True
assert _mime_matches("image/png", "text/*") == False
assert _mime_matches("application/zip", "application/zip") == True
assert _mime_matches("text/x-python", "text/*") == True
```

---

### Step 1.2 — Category lookup by MIME

**File:** `src/media.py`
**What:** Given a MIME type, find which category (if any) it belongs to.

```python
def _classify_by_mime(mime: str, categories: dict[str, CategoryConfig]) -> tuple[str, CategoryConfig] | None:
    """Return (category_name, CategoryConfig) if mime matches a category, else None."""
    for cat_name, cat_cfg in categories.items():
        for pattern in cat_cfg.mime_types:
            if _mime_matches(mime, pattern):
                return cat_name, cat_cfg
    return None
```

**Verify:**
```python
cats = {"code": CategoryConfig(mime_types=["text/*"], routing="pass")}
assert _classify_by_mime("text/x-python", cats) == ("code", cats["code"])
assert _classify_by_mime("image/png", cats) is None
```

---

### Step 1.3 — Category lookup by extension

**File:** `src/media.py`
**What:** Fallback classification when MIME is unknown or generic (`application/octet-stream`).

```python
def _classify_by_extension(filename: str, categories: dict[str, CategoryConfig]) -> tuple[str, CategoryConfig] | None:
    """Return (category_name, CategoryConfig) if filename extension matches, else None."""
    ext = Path(filename).suffix.lower()
    if not ext:
        return None
    # Longest-match-first: .tar.gz should match .gz, not .tar
    for cat_name, cat_cfg in categories.items():
        if ext in cat_cfg.extensions:
            return cat_name, cat_cfg
    return None
```

**Verify:**
```python
cats = {"archive": CategoryConfig(extensions=[".zip", ".tar.gz"], routing="warn")}
assert _classify_by_extension("bundle.zip", cats) == ("archive", cats["archive"])
assert _classify_by_extension("readme.md", cats) is None
```

---

### Step 1.4 — Combined classifier with size check

**File:** `src/media.py`
**What:** Single entry point that classifies a file by MIME first, then extension fallback, then checks size.

```python
def classify_file(
    mime: str,
    filename: str,
    file_size: int,
    categories: dict[str, CategoryConfig],
    default_routing: str = "block",
) -> tuple[str, str, str]:
    """Classify a file and return (category, routing, reason).
    
    Priority:
    1. MIME type match (most reliable)
    2. Extension fallback (for generic MIMEs like octet-stream)
    3. Default routing (catch-all)
    
    Size check applied after category match.
    """
    # Try MIME first
    result = _classify_by_mime(mime, categories)
    if result is None:
        # Fall back to extension
        result = _classify_by_extension(filename, categories)
    
    if result is not None:
        cat_name, cat_cfg = result
        if file_size > cat_cfg.max_size_bytes:
            return cat_name, "block", f"exceeds {cat_cfg.max_size_bytes // 1_048_576}MB limit"
        return cat_name, cat_cfg.routing, ""
    
    return "unknown", default_routing, ""
```

**Verify:**
```python
cats = {"code": CategoryConfig(mime_types=["text/*"], extensions=[".py"], max_size_bytes=10_000_000, routing="pass")}
# MIME match
assert classify_file("text/x-python", "main.py", 5000, cats) == ("code", "pass", "")
# Extension fallback
assert classify_file("application/octet-stream", "main.py", 5000, cats) == ("code", "pass", "")
# Too large
assert classify_file("text/x-python", "main.py", 15_000_000, cats) == ("code", "block", "exceeds 9MB limit")
# No match
assert classify_file("application/x-msdownload", "virus.exe", 5000, cats) == ("unknown", "block", "")
```

---

## Phase 2: Routing Actions (3 steps)

### Step 2.1 — Download function with error handling

**File:** `src/media.py`
**What:** Wrap the existing `download_document`/`download_photo` in a single safe downloader.

```python
async def _safe_download(tg: "TelegramClient", file_id: str, chat_id: int, fname: str) -> bytes | None:
    """Download file with user-facing error on failure. Returns None if failed."""
    try:
        return await tg.get_file(file_id)
    except Exception as exc:
        await tg.send_message(chat_id, f"⚠️ Download failed for {fname}: {exc}")
        return None
```

**Verify:** Integration test with a real file download.

---

### Step 2.2 — Route action: `block`

**File:** `src/media.py`
**What:** Notify user and return `None` (attachment excluded from prompt).

```python
async def _route_block(tg: "TelegramClient", chat_id: int, fname: str, category: str, reason: str = "") -> None:
    """Notify user that file was blocked."""
    msg = f"⛔ Blocked: {fname}"
    if category != "unknown":
        msg += f" ({category})"
    if reason:
        msg += f" — {reason}"
    await tg.send_message(chat_id, msg)
```

**Verify:** Call with a fake file → user receives "⛔ Blocked: virus.exe (unknown)".

---

### Step 2.3 — Route action: `pass` / `warn` / `hold`

**File:** `src/media.py`
**What:** Three routing outcomes unified into one handler that returns the prompt fragment (or None for hold).

```python
async def _route_save_and_prompt(
    routing: str,
    tg: "TelegramClient",
    chat_id: int,
    file_id: str,
    fname: str,
    category: str,
    workdir: str,
    inbox_cfg: InboxConfig,
) -> str | None:
    """Execute routing action. Returns prompt fragment or None.
    
    pass  → save + include in prompt (no notification)
    warn  → save + include + warning notification
    hold  → save + notify + text-only prompt (returns None)
    """
    data = await _safe_download(tg, file_id, chat_id, fname)
    if data is None:
        return None  # download failed
    
    path = save_to_inbox(workdir, fname, data, inbox_cfg.max_total_bytes)
    size_kb = len(data) // 1024
    
    if routing == "pass":
        return f"[File: {path} — {size_kb}KB]"
    elif routing == "warn":
        await tg.send_message(chat_id, f"⚠️ Accepted with caution: {fname} ({category})")
        return f"[File ({category}): {path} — {size_kb}KB]"
    elif routing == "hold":
        await tg.send_message(chat_id, f"📎 {fname} ({size_kb}KB) saved to inbox. Not processed this turn.")
        return None
    return None
```

**Verify:** Integration test — send a `.zip` file → expect "⚠️ Accepted with caution" reply.

---

## Phase 3: Message Pipeline Integration (3 steps)

### Step 3.1 — Unified `build_media_prompt` with routing

**File:** `src/media.py`
**What:** Replace the existing `build_media_prompt` with one that routes through the safety layer.

```python
async def build_media_prompt(
    msg, tg, state, cfg,
) -> str | None:
    """Build prompt from text + routed media. Returns None if all content blocked."""
    parts = [msg.text] if msg.text else []
    wd = state.chat_dir
    
    if msg.photo and state.photo_enabled:
        largest = max(msg.photo, key=lambda p: p.get("file_size", 0))
        fsize = largest.get("file_size", 0)
        if fsize > cfg.safety.media.max_photo_bytes:
            await tg.send_message(msg.chat_id, "📸 Photo too large")
        else:
            data = await _safe_download(tg, largest["file_id"], msg.chat_id, "photo.jpg")
            if data:
                path = save_to_inbox(wd, f"photo_{largest['file_id'][:12]}.jpg", data, cfg.safety.inbox.max_total_bytes)
                parts.append(f"[Photo: {path} — {len(data)//1024}KB]")
    
    if msg.document:
        doc = msg.document
        fname = doc.get("file_name", "unknown")
        mime = doc.get("mime_type", "")
        fsize = doc.get("file_size", 0)
        
        category, routing, reason = classify_file(
            mime, fname, fsize,
            cfg.safety.media.categories,
            cfg.safety.media.default_routing,
        )
        
        if routing == "block":
            await _route_block(tg, msg.chat_id, fname, category, reason)
        elif routing in ("pass", "warn", "hold"):
            prompt = await _route_save_and_prompt(
                routing, tg, msg.chat_id, doc["file_id"],
                fname, category, wd, cfg.safety.inbox,
            )
            if prompt:
                parts.append(prompt)
    
    return " ".join(parts) if parts else None
```

**Verify:** Integration test — send `.zip` (expect warn), `.exe` (expect block), `.py` (expect pass).

---

### Step 3.2 — Remove old hardcoded functions

**File:** `src/media.py`
**What:** Delete or deprecate `_handle_photo`, `_handle_document`, `is_allowed_document`, `is_allowed_image`, and the old `ALLOWED_*_MIMES` constants. These are replaced by Step 3.1.

**Verify:** `grep -rn "ALLOWED_DOC_MIMES\|ALLOWED_IMAGE_MIMES\|is_allowed_document\|is_allowed_image" src/` → no results.

---

### Step 3.3 — Wire `clean_inbox` into the main loop

**File:** `src/daemon.py`
**What:** After each processed turn, clean the inbox. Already present in old `_handle_photo`/`_handle_document` — move it to the `_process_text` function so it runs after every turn regardless of media.

```python
# In _process_text, after the agy turn completes:
from src.media import clean_inbox
clean_inbox(state.chat_dir, cfg.safety.inbox.max_age_hours)
```

**Verify:** Check inbox directory after several turns → files older than `max_age_hours` are gone.

---

## Phase 4: Rate Limiting (2 steps)

### Step 4.1 — Per-user rate limiter

**File:** `src/queue.py`
**What:** Add rate limit tracking to the existing `TurnQueue`. Reuse the queue's existing slot mechanism; just add a pre-check.

```python
from collections import deque
import time

class RateLimit:
    __slots__ = ("timestamps",)
    def __init__(self):
        self.timestamps: deque[float] = deque()

class TurnQueue:
    def __init__(self, queue_cfg: QueueConfig):
        self.max_depth = queue_cfg.max_depth
        self.max_per_user = queue_cfg.max_per_user
        self.cooldown = queue_cfg.cooldown_seconds
        self._limits: dict[int, RateLimit] = {}
        # ... existing fields ...
    
    def check_ratelimit(self, user_id: int) -> tuple[bool, int]:
        """Returns (allowed, wait_seconds)."""
        now = time.time()
        rl = self._limits.setdefault(user_id, RateLimit())
        window = self.cooldown * 2
        while rl.timestamps and rl.timestamps[0] < now - window:
            rl.timestamps.popleft()
        if len(rl.timestamps) >= self.max_per_user:
            wait = int(rl.timestamps[0] + window - now) + 1
            return False, max(wait, 0)
        rl.timestamps.append(now)
        return True, 0
```

**Verify:**
```python
q = TurnQueue(QueueConfig(max_per_user=2, cooldown_seconds=5))
assert q.check_ratelimit(1) == (True, 0)
assert q.check_ratelimit(1) == (True, 0)
assert q.check_ratelimit(1) == (False, 5)  # third message blocked
```

---

### Step 4.2 — Rate limit check in the main loop

**File:** `src/daemon.py`
**What:** Before processing a message, check rate limits. Send cooldown reply if throttled.

```python
# In run(), inside the update loop, before _process_text:
allowed, wait = _QUEUE.check_ratelimit(msg.user_id)
if not allowed:
    await tg.send_message(msg.chat_id, f"⏳ Rate limit. Wait {wait}s.")
    continue
```

**Verify:** Integration test — send 6 rapid messages → 6th gets rate limit reply.

---

## Phase 5: Size-Capped Inbox (2 steps)

### Step 5.1 — Size-aware `save_to_inbox`

**File:** `src/media.py`
**What:** Overwrite existing `save_to_inbox` with one that enforces total inbox size.

```python
def save_to_inbox(workdir: str, filename: str, data: bytes, max_total_bytes: int = 524_288_000) -> Path:
    inbox = Path(workdir) / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)
    
    # Enforce total size cap
    files = sorted(inbox.glob("*"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while total + len(data) > max_total_bytes and files:
        oldest = files.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()
    
    ts = int(time.time())
    dest = inbox / f"{ts}_{filename}"
    dest.write_bytes(data)
    return dest
```

**Verify:** Fill inbox to near limit, save new file → oldest files are purged, total ≤ max.

---

### Step 5.2 — Inbox admin commands

**File:** `src/commands.py`
**What:** Add `/files` and `/files clean` commands.

```python
# In handle_text_command, add:
if cmd == "/files":
    if args == "clean":
        removed = clean_inbox(cs.chat_dir, max_age_hours=0)  # 0 = remove all
        return BridgeReply(f"🧹 Inbox cleaned: {removed} files removed")
    files = list_inbox(cs.chat_dir, limit=20)
    if not files:
        return BridgeReply("📭 Inbox is empty")
    return BridgeReply("📎 Inbox:\n" + "\n".join(f"• {f}" for f in files))
```

**Verify:** `/files` lists inbox. `/files clean` empties it. `/files clean` again → "0 files removed".

---

## Phase 6: Config Migration (1 step)

### Step 6.1 — Write production `config.json` with safety defaults

**File:** `config.json`
**What:** Add the full `safety` block from the architecture document Section 1.2 to the active config. Bridge restart picks it up.

**Verify:** After restart, `curl http://127.0.0.1:9100/` shows memory config. Send a `.zip` file → receives warn notification.

---

## Phase 7: Final Integration Test (1 step)

### Step 7.1 — End-to-end test matrix

Run each scenario and verify the expected outcome:

| # | Scenario | Expected |
|---|---|---|
| 1 | Send `.py` file (5KB) | `pass` — included in prompt, no warning |
| 2 | Send `.zip` file (2MB) | `warn` — "Accepted with caution", included |
| 3 | Send `.dwg` file (1MB) | `warn` — "Accepted with caution", included |
| 4 | Send `.docx` file (500KB) | `warn` — "Accepted with caution", included |
| 5 | Send `.exe` file | `block` — "⛔ Blocked: file.exe (unknown)" |
| 6 | Send `.mp3` file (3MB) | `hold` — "📎 saved to inbox. Not processed" |
| 7 | Send `.py` file (60MB) | `block` — "exceeds XMB limit" |
| 8 | Send 6 text-only messages in 2 seconds | 6th → "⏳ Rate limit. Wait Ns." |
| 9 | Fill inbox over 500MB, send new file | Oldest files purged, new file saved |
| 10 | `/files` command | Lists inbox contents |
| 11 | `/files clean` command | Empties inbox |
| 12 | Run bridge 24 hours | Memory stabilises (no unbounded growth) |

---

## Implementation Order & Dependencies

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6 ──► Phase 7
  │           │           │           │           │           │           │           │
  │           │           │           │           │           │           │           │
  config      classify    route       prompt      rate        inbox      deploy      test
  types       logic       actions     integration limit       cap        config      matrix
```

Each phase depends on the previous. Within a phase, steps are sequential.

**Total: 18 steps across 7 phases. Estimated implementation time: 4–6 hours.**
