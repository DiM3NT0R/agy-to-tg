# Roadmap & TODO

## 🚀 Предстоящие задачи (Upcoming Features)

### 1. Поддержка OpenAI-совместимых API провайдеров (`OpenAI-Compatible Engine`)
Создание третьего движка в дополнение к `agy` и `codex` для прямого подключения любых сторонних и локальных моделей через стандартный `/v1/chat/completions` API.

#### 1.1. Поддерживаемые провайдеры и бэкенды
- [ ] **OpenRouter** — доступ к Claude, Gemini, Llama 3, Mistral, Command-R через единый ключ.
- [ ] **DeepSeek API** — официальный API для моделей DeepSeek-Coder / DeepSeek-V3 / DeepSeek-R1.
- [ ] **Локальные серверы** (Self-hosted & Local LLMs):
  - [ ] **Ollama** (`http://localhost:11434/v1`)
  - [ ] **vLLM** (`http://localhost:8000/v1`)
  - [ ] **LM Studio** / **LocalAI** (`http://localhost:1234/v1`)
- [ ] **Groq Cloud** — сверхбыстрый инференс Llama 3 / Mixtral.

#### 1.2. Архитектура и реализация движка
- [ ] **Конфигурация провайдеров (`config.json`):**
  ```json
  "openai_compatible": {
    "providers": {
      "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-...",
        "default_model": "anthropic/claude-3.5-sonnet"
      },
      "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-...",
        "default_model": "deepseek-reasoner"
      },
      "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "default_model": "qwen2.5-coder:32b"
      }
    }
  }
  ```
- [ ] **Потоковый клиент (`src/openai_compat_runner.py`):**
  - Поддержка Server-Sent Events (SSE) потокового вывода `/v1/chat/completions`.
  - Трансляция reasoning / thinking токенов (например, `<think>` теги у DeepSeek R1 или поле `reasoning_content`) в `StatusUpdater`.
  - Захват usage-статистики (prompt_tokens, completion_tokens, reasoning_tokens).
- [ ] **Автономные инструменты (Tool / Function Calling):**
  - Реализация системного исполнителя команд (`run_command`), чтения (`read_file`) и записи файлов (`write_file`) через стандартные JSON Schema tools.
  - Безопасная изоляция в рабочей директории чата (`cwd = chats_root / chat_id`).
- [ ] **Динамическое обнаружение моделей:**
  - Запрос к `GET {base_url}/models` для автоматического наполнения меню `/model` и `/settings`.
- [ ] **Бесшовный переход между движками (Context Compactor):**
  - Интеграция с `src/context_compactor.py` для сохранения контекста при переключении между `agy` ↔ `codex` ↔ `openai-compat`.

---

### 2. Улучшение подписочной модели и управления сессиями
- [ ] **Мульти-аккаунтинг:** поддержка переключения между несколькими Google и OpenAI аккаунтами.
- [ ] **Авто-продление сессий:** отслеживание времени жизни токенов и фоновый refresh без участия пользователя.
- [ ] **Мониторинг квот подписок:** отдельное отображение лимитов (Claude Opus в agy, GPT-4o/o3 в codex) в команде `/status`.

---

### 3. Telegram UI & UX
- [ ] **Интерактивные чекбоксы для мультивыбора:** поддержка вопросов, где можно выбрать несколько вариантов одновременно.
- [ ] **Web App / Mini App Dashboard:** легковесный Mini App для визуального просмотра diff'ов, файлов проекта и статуса задач.
- [ ] **Экспорт сессии:** команда `/export` для скачивания истории диалога и созданных файлов в виде ZIP-архива.
