# Agent TG Bridge (`agent-tg-bridge`)

[English](#english) | [Русский](#русский)

---

<a name="english"></a>
## English

**Agent TG Bridge** is a feature-rich, asynchronous Telegram gateway for interacting with autonomous coding AI agents and CLI tools directly from your messenger.

Unlike conventional API wrappers that charge per-token, this bridge enables using your existing **flat-rate subscriptions** (such as **Google Antigravity** and **ChatGPT Plus/Pro / OpenAI Codex**) via official CLI tooling, alongside support for self-hosted and third-party **OpenAI-compatible APIs**.

### 🌟 Core Highlights

* **Multi-Engine Architecture:**
  * **Google Antigravity (`agy`):** Access Gemini 3.8 Flash, Gemini 4 Argon, Claude Opus 5.5 High via Google account subscriptions.
  * **OpenAI Codex (`codex`):** Access GPT-6.1 Sol, GPT-6 Astra, GPT-5.6 Luna, o3 via ChatGPT Plus/Pro device OAuth.
  * **OpenAI-Compatible APIs (Roadmap):** Direct connection to OpenRouter, DeepSeek, Ollama, vLLM, LM Studio.
* **Subscription-Powered (Zero Per-Token Bills):**
  * Seamlessly authenticate ChatGPT Plus/Pro via `/auth` directly in Telegram using the interactive Device Code flow.
  * Google OAuth CLI session auto-refresh.
* **Interactive Live Thinking & Checkpoints:**
  * Real-time reasoning and execution checkpoint streaming in Telegram status messages without cluttering the chat.
  * Collapsible expandable blockquotes (`<blockquote expandable>`) for thought history.
* **Interactive Quick-Reply Question Buttons:**
  * Automatically detects multiple-choice questions asked by the model (`• Option 1`, `1. Option 2`, etc.).
  * Renders 1-tap inline buttons in real time during live execution (via `codex queue`) and on completed answers.
* **Cross-Engine Token Handoff & Context Compaction:**
  * Switch models or engines mid-chat (`/model` or `/engine`) without losing context — automatic summary distillation preserves project continuity.
* **Voice-First Experience:**
  * Voice message transcription via Groq Whisper (`whisper-large-v3-turbo`).
  * Text-to-speech voice replies via Cartesia or Yandex SpeechKit.
* **Strict Session Isolation & Security:**
  * Dedicated working directory per chat and forum topic (`supergroup`).
  * Whitelist-based access control (`allowed_user_ids`, `allowed_chat_ids`).
  * Process group termination, zombie reaping, and OOM protection.

### 🛠 Quick Start

#### Requirements
- Linux (x86_64 / aarch64)
- Python ≥ 3.11
- Pre-installed `agy` and/or `codex` CLI tools in `$PATH`
- `uv` package manager

#### Installation
```bash
# Clone the repository
git clone https://github.com/DiM3NT0R/agent-tg-bridge.git ~/.gemini/extensions/antigravity-telegram-bridge
cd ~/.gemini/extensions/antigravity-telegram-bridge

# Run installation script (sets up virtualenv & dependencies)
./install.sh

# Configure settings
cp config.example.json config.json
chmod 600 config.json
nano config.json
```

#### Starting the Service
```bash
systemctl --user enable --now antigravity-telegram-bridge.service
# Ensure user lingering is enabled: loginctl enable-linger $USER
```

### 💬 Key Bot Commands

| Command | Description |
| :--- | :--- |
| `/settings` | Graphical menu for engine, model, thinking mode, effort, and timeouts |
| `/model [name]` | Quick-switch active AI model or view available models |
| `/engine [agy\|codex]` | Switch between Antigravity and Codex engines |
| `/account` (`/auth`) | Multi-account manager for OpenAI Codex: 1-click account switching, OAuth login |
| `/codex_key [sk-...]` | Quickly set or add an OpenAI API key (auto-deletes key message from chat) |
| `/thinking [live\|all\|off]` | Configure reasoning stream display |
| `/voice` | Configure voice synthesis settings (speed, voice actor, auto-voice) |
| `/status` | Current session diagnostics, active engine, token usage, and working directory |
| `/reset` | Terminate current session and start fresh |
| `/files` | Manage and inspect workspace files |

---

<a name="русский"></a>
## Русский

**Agent TG Bridge** (ранее `agy-to-tg`) — универсальный асинхронный Telegram-мост для работы с автономными AI-агентами разработки и CLI-инструментами прямо из мессенджера.

Главное преимущество моста — возможность использовать ваши **существующие подписки** (**Google Antigravity** и **ChatGPT Plus/Pro / OpenAI Codex**) через официальные CLI без платы за токены, а также подключение локальных и облачных **OpenAI-совместимых API**.

### 🌟 Ключевые возможности

* **Мульти-движковая архитектура:**
  * **Google Antigravity (`agy`):** Доступ к Gemini 3.8 Flash, Gemini 4 Argon, Claude Opus 5.5 High по подписке Google.
  * **OpenAI Codex (`codex`):** Доступ к GPT-6.1 Sol, GPT-6 Astra, GPT-5.6 Luna, o3 по подписке ChatGPT Plus/Pro.
  * **OpenAI-Compatible API (в разработке):** Подключение OpenRouter, DeepSeek, Ollama, vLLM, LM Studio.
* **Работа по подписке (без счетов за API-токены):**
  * Интерактивная авторизация в ChatGPT Plus/Pro прямо через Telegram по команде `/auth` (Device Code flow).
  * Фоновое обновление OAuth-токенов Google через CLI.
* **Живые размышления (Live Thinking):**
  * Модель транслирует свои чекпоинты и размышления в реальном времени прямо в статус-сообщение Telegram.
  * Поддержка сворачиваемых цитат (`<blockquote expandable>`) для итогового отображения хода мысли.
* **Интерактивные кнопки вопросов:**
  * Автоматическое распознавание вопросов с вариантами ответа (`• Вариант 1`, `1. Вариант 2` и т.д.).
  * Вывод инлайн-кнопок прямо в процессе выполнения (через `codex queue`) и под итоговым ответом для выбора в 1 клик.
* **Бесшовный переход между моделями (Context Compactor):**
  * Смена движков и моделей прямо посреди диалога без потери контекста проекта — суммаризация и эстафета истории.
* **Голосовое управление:**
  * Распознавание входящих голосовых через Groq Whisper (`whisper-large-v3-turbo`).
  * Озвучка ответов голосом через Cartesia или Yandex SpeechKit.
* **Изоляция и безопасность:**
  * Отдельная рабочая директория под каждый чат и топик супергруппы.
  * Контроль доступа по белым спискам (`allowed_user_ids`, `allowed_chat_ids`).
  * Защита от OOM, утечек памяти и сборка зомби-процессов.

### 🛠 Установка и запуск

#### Требования
- Linux (x86_64 / aarch64)
- Python ≥ 3.11
- Установленные утилиты `agy` и/или `codex` в `$PATH`
- Пакетный менеджер `uv`

#### Развёртывание
```bash
git clone https://github.com/DiM3NT0R/agent-tg-bridge.git ~/.gemini/extensions/antigravity-telegram-bridge
cd ~/.gemini/extensions/antigravity-telegram-bridge
./install.sh

cp config.example.json config.json
chmod 600 config.json
nano config.json
```

#### Запуск службы
```bash
systemctl --user enable --now antigravity-telegram-bridge.service
# Включите linger для работы сервиса в фоне: loginctl enable-linger $USER
```

### 💬 Основные команды

| Команда | Описание |
| :--- | :--- |
| `/settings` | Интерактивное меню настроек (выбор движка, модели, режима размышлений, таймаутов) |
| `/model [название]` | Быстрое переключение модели или просмотр доступного списка |
| `/engine [agy\|codex]` | Переключение активного движка (Antigravity или Codex) |
| `/account` (`/auth`) | Менеджер аккаунтов Codex: переключение в 1 клик, профили, OAuth вход |
| `/codex_key [sk-...]` | Добавление OpenAI API ключа (сообщение с ключом авто-удаляется из чата) |
| `/thinking [live\|all\|off]` | Настройка отображения хода размышлений модели |
| `/voice` | Настройка голосового вывода (скорость, диктор, авто-озвучка) |
| `/status` | Сводка по активной сессии, токенам, памяти и рабочей папке |
| `/reset` | Принудительный перезапуск текущей сессии с чистого листа |
| `/files` | Управление файлами в рабочей директории |

### 🗺 Планы развития (Roadmap)
Подробный план интеграции OpenAI-совместимых провайдеров и локальных моделей см. в файле [TODO.md](file:///root/.gemini/extensions/antigravity-telegram-bridge/TODO.md).
