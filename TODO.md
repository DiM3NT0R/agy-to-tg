# Roadmap & TODO

## 🚀 Предстоящие задачи (Upcoming Features)

### 1. Поддержка OpenAI-совместимых API провайдеров (`OpenAI-Compatible Engine`) ✅ Реализовано
Универсальный третий движок в дополнение к `agy` и `codex` для прямого подключения любых сторонних и локальных моделей через стандартный `/v1/chat/completions` API (CeapAI, OpenRouter, DeepSeek, Ollama, Groq, vLLM).

#### 1.1. Поддерживаемые провайдеры и бэкенды
- [x] **CeapAI (cheapai.io)** — агрегатор топовых моделей (Claude, GPT, Gemini, Grok) с единым ключом.
- [x] **OpenRouter** — доступ к Claude, Gemini, Llama 3, Mistral, Command-R через единый ключ.
- [x] **DeepSeek API** — официальный API для моделей DeepSeek-Coder / DeepSeek-V3 / DeepSeek-R1.
- [x] **Локальные серверы** (Self-hosted & Local LLMs): Ollama, vLLM, LM Studio, LocalAI.
- [x] **Groq Cloud** — сверхбыстрый инференс Llama 3 / Mixtral.

#### 1.2. Архитектура и реализация движка
- [x] **Универсальная форма настройки провайдеров в Telegram (`src/openai_providers.py`, `src/commands.py`):**
  - Интерактивное меню управления в `/settings ➔ 🌐 API Провайдеры` (`nav:providers`).
  - Команда `/provider add <id> <base_url> <api_key> [model]` (и быстрые пресеты `/provider add ceapai <key>`).
  - Быстрое переключение активного провайдера в 1 клик, безопасное автоудаление сообщений с ключами.
- [x] **Потоковый клиент с инструментами (`src/openai_compat_runner.py`):**
  - Поддержка Server-Sent Events (SSE) потокового вывода `/v1/chat/completions`.
  - Трансляция reasoning / thinking токенов (DeepSeek R1 reasoning_content и `<think>` теги) в `StatusUpdater`.
  - Захват usage-статистики (prompt_tokens, completion_tokens, reasoning_tokens).
  - Автономные инструменты рабочей директории (`run_command`, `read_file`, `write_file`).
- [x] **Динамическое обнаружение моделей:**
  - Запрос к `GET {base_url}/models` для автоматического наполнения меню `/model` и `/provider models`.
- [x] **Бесшовный переход между движками (Context Compactor):**
  - Интеграция с `src/context_compactor.py` при переключении `agy` ↔ `codex` ↔ `openai`.

---

### 2. Улучшение подписочной модели и отображения лимитов
- [ ] **Отображение лимитов и квот OpenAI Codex в `/limits`:**
  - Поддержка отображения квот и rate limits для подписок ChatGPT Plus/Pro (остаток запросов o3, GPT-4o/GPT-6, таймеры сброса).
  - Контекстное отображение лимитов: если в чате активен движок `codex`, показывать лимиты OpenAI; если `agy` — лимиты Google Antigravity (Gemini, Claude Opus). Либо комбинированная вкладка с переключением между провайдерами.
  - Извлечение информации об остатке квот из заголовков ответов Codex (`x-ratelimit-*`) или через внутренний статус сессии/OAuth-токены в `~/.codex/auth.json`.
- [ ] **Мульти-аккаунтинг:** поддержка переключения между несколькими Google и OpenAI аккаунтами.
- [ ] **Авто-продление сессий:** отслеживание времени жизни токенов и фоновый refresh без участия пользователя.
- [ ] **Мониторинг квот подписок:** отображение остатка лимитов в команде `/status`.

---

### 3. Telegram UI & UX
- [ ] **Интерактивные чекбоксы для мультивыбора:** поддержка вопросов, где можно выбрать несколько вариантов одновременно.
- [ ] **Web App / Mini App Dashboard:** легковесный Mini App для визуального просмотра diff'ов, файлов проекта и статуса задач.
- [ ] **Экспорт сессии:** команда `/export` для скачивания истории диалога и созданных файлов в виде ZIP-архива.
