# Agy Bridge — Safety Layer Architecture & Implementation Plan

## Document Control

| Field | Value |
|---|---|
| Document ID | AGY-SAFETY-ARCH-001 |
| Version | 1.0 |
| Date | 2026-07-18 |
| Author | HH |
| Status | Draft for Review |

---

## 1. Solution Architecture

### 1.1 Overview

The Agy Telegram Bridge currently operates with a fixed allowlist for file types, sizes, and queue depth. The proposed architecture replaces hardcoded allowlists with a **configurable three-tier safety pipeline** — Gate → Classify → Route. Each tier can be tuned via `config.json` without code changes.

```
Inbound Message (Telegram)
    │
    ▼
┌─────────────────────────────────────┐
│  TIER 1: GATE                        │
│  - User auth (already present)       │
│  - Queue depth check                 │
│  - Rate limiting (new)               │
│  - Anti-spam (new)                   │
├─────────────────────────────────────┤
│  TIER 2: CLASSIFY                    │
│  - MIME type → category mapping      │
│  - File size → tier mapping          │
│  - Extension inference (no MIME)     │
│  - Binary detection (new)            │
├─────────────────────────────────────┤
│  TIER 3: ROUTE                       │
│  - Pass-through (allowed)            │
│  - Warn + allow (risky but permitted)│
│  - Hold for approval (sensitive)     │
│  - Block (forbidden)                 │
│  - Strip attachment (text-only fallback) │
└─────────────────────────────────────┘
    │
    ▼
Agy CLI (process turn)
```

### 1.2 Configuration Schema (`config.json`)

```json
{
  "telegram": {
    "bot_token": "<token>",
    "allowed_user_ids": [8145172607],
    "allowed_chat_ids": [8145172607]
  },
  "agy": {
    "chats_root": "",
    "default_workdir": "/home/i",
    "model": "",
    "mode": "code"
  },
  "safety": {
    "media": {
      "max_photo_bytes": 20971520,
      "max_file_bytes": 52428800,
      "max_stdout_bytes": 524288,
      "categories": {
        "code": {
          "mime_types": ["text/*", "application/json", "application/x-yaml", "application/xml"],
          "extensions": [".py", ".js", ".ts", ".go", ".rs", ".rb", ".java", ".c", ".cpp", ".h", ".php", ".ipynb", ".sh", ".bash", ".zsh"],
          "max_size_bytes": 10485760,
          "routing": "pass"
        },
        "documents": {
          "mime_types": ["application/pdf", "text/markdown", "text/csv", "text/plain"],
          "extensions": [".pdf", ".md", ".txt", ".csv", ".rst", ".tex"],
          "max_size_bytes": 52428800,
          "routing": "pass"
        },
        "office": {
          "mime_types": ["application/vnd.openxmlformats-officedocument.*", "application/msword", "application/vnd.ms-excel"],
          "extensions": [".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"],
          "max_size_bytes": 20971520,
          "routing": "warn"
        },
        "archives": {
          "mime_types": ["application/zip", "application/x-tar", "application/gzip", "application/x-7z-compressed"],
          "extensions": [".zip", ".tar", ".gz", ".tgz", ".7z", ".bz2"],
          "max_size_bytes": 52428800,
          "routing": "warn"
        },
        "cad": {
          "mime_types": ["application/octet-stream", "application/x-dxf", "image/vnd.dwg"],
          "extensions": [".dwg", ".dxf", ".svg", ".iges", ".step", ".stp", ".ifc"],
          "max_size_bytes": 52428800,
          "routing": "warn"
        },
        "images": {
          "mime_types": ["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff", "image/svg+xml"],
          "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".svg", ".heic"],
          "max_size_bytes": 20971520,
          "routing": "pass"
        },
        "data": {
          "mime_types": ["application/sqlite3", "application/x-sqlite3", "application/octet-stream"],
          "extensions": [".db", ".sqlite", ".sqlite3", ".sql", ".parquet", ".feather", ".arrow"],
          "max_size_bytes": 20971520,
          "routing": "warn"
        },
        "notebooks": {
          "mime_types": ["application/json", "application/octet-stream"],
          "extensions": [".ipynb"],
          "max_size_bytes": 10485760,
          "routing": "pass"
        },
        "media": {
          "mime_types": ["audio/*", "video/*"],
          "extensions": [".mp3", ".wav", ".ogg", ".flac", ".mp4", ".webm", ".mov"],
          "max_size_bytes": 52428800,
          "routing": "hold"
        }
      },
      "default_routing": "block"
    },
    "queue": {
      "max_depth": 10,
      "max_per_user": 5,
      "cooldown_seconds": 2
    },
    "memory": {
      "limit_bytes": 805306368,
      "check_interval_loops": 30
    },
    "inbox": {
      "max_age_hours": 24,
      "max_total_bytes": 524288000
    }
  }
}
```

### 1.3 Routing Actions

| Action | Behavior | User Notification |
|---|---|---|
| `pass` | File saved to inbox, included in prompt | None |
| `warn` | File saved, included, but warning appended to prompt | "⚠️ File type X accepted with caution" |
| `hold` | File saved, prompt proceeds WITHOUT attachment | "📎 File saved but not processed. Send /files to manage." |
| `block` | File rejected | "⛔ Unsupported file: {filename}" |
| `strip` | File ignored, text-only prompt | None (silent) |

---

## 2. Workflows & Interaction Interfaces

### 2.1 Message Processing Pipeline

```
┌──────────────────────────────────────────────────────────────┐
│                     MESSAGE PIPELINE                          │
│                                                              │
│  Telegram Update                                             │
│      │                                                       │
│      ▼                                                       │
│  ┌─────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐ │
│  │ Parse   │───►│ Authorize│───►│ Dequeue  │───►│Classify │ │
│  │ Update  │    │ User     │    │ Check    │    │Content  │ │
│  └─────────┘    └──────────┘    └──────────┘    └─────────┘ │
│                      │               │               │       │
│                 ┌────▼────┐    ┌────▼────┐    ┌────▼────┐   │
│                 │Block    │    │Queue    │    │Route    │   │
│                 │Reply    │    │Reply    │    │Decision │   │
│                 └─────────┘    └─────────┘    └────┬────┘   │
│                                                   │         │
│                          ┌────────────────────────┤         │
│                          ▼            ▼           ▼          │
│                      ┌──────┐   ┌──────┐   ┌──────────┐    │
│                      │Pass  │   │Warn  │   │Hold/Strip│    │
│                      │→ Agy │   │→ Agy │   │→ Agy     │    │
│                      └──────┘   └──────┘   │(text-only)│    │
│                                            └──────────┘    │
│                          │            │           │         │
│                          └────────────┴───────────┘         │
│                                      │                       │
│                                      ▼                       │
│                              ┌──────────────┐               │
│                              │ Agy CLI Turn │               │
│                              │ (subprocess) │               │
│                              └──────┬───────┘               │
│                                     │                        │
│                                     ▼                        │
│                              ┌──────────────┐               │
│                              │Reply →       │               │
│                              │Telegram      │               │
│                              └──────────────┘               │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 Rate Limiting Workflow

```
Inbound per-user counter (sliding window)
    │
    ├── Messages in window > max_per_user?
    │       │
    │       ├── YES → Cooldown reply: "⏳ Rate limited. Wait {N}s."
    │       │
    │       └── NO → Proceed to classification
    │
    └── Window: cooldown_seconds × 2 (rolling)
```

### 2.3 Inbox Lifecycle

```
Inbox directory: {chat_dir}/.bridge-inbox/
    │
    ├── On file save:
    │       ├── Save with timestamp prefix: {ts}_{filename}
    │       └── Check total inbox size → purge oldest if > max_total_bytes
    │
    ├── After each turn:
    │       └── clean_inbox() — remove files older than max_age_hours
    │
    └── User commands:
            ├── /files         → list inbox contents
            ├── /files clean   → purge all inbox files
            └── /files keep N  → keep N most recent, purge rest
```

### 2.4 Memory Health Loop

```
Main event loop (every 30 iterations)
    │
    ├── Read /proc/self/statm → RSS bytes
    │
    ├── RSS > 768 MiB?
    │       │
    │       ├── YES → LOG.critical → stop_event.set() → graceful exit
    │       │                                        │
    │       │                        systemd Restart=on-failure
    │       │                                        │
    │       │                        Clean process starts (20MB RSS)
    │       │
    │       └── NO → Continue
    │
    └── After each subprocess turn:
            ├── os.waitpid(-1, WNOHANG) → reap zombies
            └── gc.collect() → free Python heap
```

---

## 3. Implementation Plan

### Phase 1: Configurable Media Safety Layer (P0 — 2 hours)

**File:** `src/media.py` + `src/config.py`

#### Step 1.1: Extend Config Schema

Add `SafetyConfig` dataclass to `src/config.py`:

```python
@dataclass
class CategoryConfig:
    mime_types: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    max_size_bytes: int = 52_428_800
    routing: str = "block"  # pass | warn | hold | block | strip

@dataclass
class MediaSafetyConfig:
    max_photo_bytes: int = 20_971_520
    max_file_bytes: int = 52_428_800
    max_stdout_bytes: int = 524_288
    categories: dict[str, CategoryConfig] = field(default_factory=dict)
    default_routing: str = "block"

@dataclass
class QueueConfig:
    max_depth: int = 10
    max_per_user: int = 5
    cooldown_seconds: int = 2

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

#### Step 1.2: Implement Category Classifier

Replace hardcoded `ALLOWED_DOC_MIMES` with config-driven classification:

```python
def classify_file(mime: str, filename: str, size: int, cfg: SafetyConfig) -> tuple[str, str]:
    """Returns (category_name, routing_action)."""
    for cat_name, cat_cfg in cfg.media.categories.items():
        if _mime_matches(mime, cat_cfg.mime_types):
            if size > cat_cfg.max_size_bytes:
                return cat_name, "block"
            return cat_name, cat_cfg.routing
    ext = Path(filename).suffix.lower()
    for cat_name, cat_cfg in cfg.media.categories.items():
        if ext in cat_cfg.extensions:
            if size > cat_cfg.max_size_bytes:
                return cat_name, "block"
            return cat_name, cat_cfg.routing
    return "unknown", cfg.media.default_routing
```

**Validation:** Test with `.zip`, `.dwg`, `.ipynb`, `.docx` — all previously blocked. Test with unknown `.exe` — should block.

### Phase 2: Routing Logic (P0 — 1 hour)

**File:** `src/media.py`

#### Step 2.1: Implement Routing Actions

```python
async def route_media(msg, tg, state, cfg) -> str | None:
    """Classify and route media attachment. Returns prompt fragment or None."""
    category, routing = classify_file(mime, fname, fsize, cfg.safety)
    
    if routing == "block":
        await tg.send_message(msg.chat_id, f"⛔ Blocked: {fname} ({category})")
        return None
    elif routing == "hold":
        await tg.send_message(msg.chat_id, f"📎 {fname} saved to inbox. Not processed this turn.")
        data = await download(tg, file_id)
        save_to_inbox(workdir, fname, data)
        return None  # text-only this turn
    elif routing == "warn":
        await tg.send_message(msg.chat_id, f"⚠️ {fname} accepted with caution ({category})")
        data = await download(tg, file_id)
        path = save_to_inbox(workdir, fname, data)
        return f"[File ({category}): {path} — {len(data)//1024}KB]"
    else:  # pass
        data = await download(tg, file_id)
        path = save_to_inbox(workdir, fname, data)
        return f"[File: {path} — {len(data)//1024}KB]"
```

### Phase 3: Queue & Rate Limiting (P1 — 1 hour)

**File:** `src/queue.py` + `src/daemon.py`

#### Step 3.1: Per-User Rate Limiting

```python
@dataclass
class RateLimit:
    timestamps: deque[float] = field(default_factory=deque)

class TurnQueue:
    def __init__(self, cfg: SafetyConfig):
        self.max_depth = cfg.queue.max_depth
        self.max_per_user = cfg.queue.max_per_user
        self.cooldown = cfg.queue.cooldown_seconds
        self._rate_limits: dict[int, RateLimit] = {}
    
    def check_rate(self, user_id: int) -> bool:
        """Returns True if user is NOT rate-limited."""
        now = time.time()
        rl = self._rate_limits.setdefault(user_id, RateLimit())
        # Purge old entries
        while rl.timestamps and rl.timestamps[0] < now - self.cooldown * 2:
            rl.timestamps.popleft()
        if len(rl.timestamps) >= self.max_per_user:
            return False
        rl.timestamps.append(now)
        return True
```

### Phase 4: Inbox Management (P1 — 30 min)

**File:** `src/media.py`

#### Step 4.1: Size-Capped Inbox

```python
def save_to_inbox(workdir: str, filename: str, data: bytes, max_total: int = 524_288_000) -> Path:
    inbox = Path(workdir) / INBOX_DIR_NAME
    inbox.mkdir(parents=True, exist_ok=True)
    
    # Check total size and purge oldest if needed
    total = sum(f.stat().st_size for f in inbox.iterdir() if f.is_file())
    if total + len(data) > max_total:
        files = sorted(inbox.glob("*"), key=lambda f: f.stat().st_mtime)
        for f in files:
            if total + len(data) <= max_total:
                break
            total -= f.stat().st_size
            f.unlink()
    
    ts = int(time.time())
    dest = inbox / f"{ts}_{filename}"
    dest.write_bytes(data)
    return dest
```

### Phase 5: Config Migration & Testing (P1 — 1 hour)

| Step | Action |
|---|---|
| 5.1 | Write `config.example.json` with full safety schema |
| 5.2 | Add unit tests for `classify_file` with 20 MIME/extension combinations |
| 5.3 | Add unit tests for `RateLimit` check |
| 5.4 | Integration test: send `.zip` → expect `warn` routing |
| 5.5 | Integration test: send `.exe` → expect `block` |
| 5.6 | Integration test: send 6 messages in 2s → expect rate limit reply |

### Phase 6: Documentation & Deploy (P2 — 30 min)

| Step | Action |
|---|---|
| 6.1 | Update README with safety configuration guide |
| 6.2 | Add `/safety` command to show current limits |
| 6.3 | Add `agy bridge check` control command to validate config |
| 6.4 | Restart bridge, verify health endpoint shows new config |

---

## 4. Summary — Before/After

| Blocker | Before | After |
|---|---|---|
| ZIP files | ⛔ Blocked | ⚠️ Warn + accept |
| CAD files (.dwg, .dxf) | ⛔ Blocked | ⚠️ Warn + accept |
| Office (.docx, .xlsx) | ⛔ Blocked | ⚠️ Warn + accept |
| Jupyter (.ipynb) | ⛔ Blocked | ✅ Pass |
| C/C++/.java code | ⛔ Blocked | ✅ Pass |
| Media (audio/video) | ⛔ Blocked | 📎 Hold (saved, not processed) |
| Unknown file types | ⛔ Blocked with "Unsupported" | ⛔ Blocked with category info |
| File size limit | 20 MB (hardcoded) | Per-category configurable, up to 50 MB |
| Queue depth | 5 (hardcoded) | 10 (configurable) |
| Rate limiting | ❌ None | ✅ Per-user cooldown + max burst |
| Inbox cleanup | Age-based only | Age + total size cap |
| Config changes | Requires code edit | Requires `config.json` edit + restart |

---

## 5. Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Malicious ZIP bomb (.zip with nested archives) | Low | Files aren't extracted, just stored. Max 50MB. |
| Executable disguised as allowed extension | Low | `default_routing: block` catches unknown MIME. Extensions are secondary. |
| Config misparse crashes bridge | Low | `ConfigError` exception caught at startup. Invalid config → bridge won't start. |
| Memory ceiling too aggressive | Low | 768 MiB is 38× typical RSS. Override via `AGY_BRIDGE_MEMORY_LIMIT` env var. |
