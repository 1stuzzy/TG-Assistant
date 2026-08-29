# TG-Assistant

## Структура проекта

```
TG-Assistant/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── auth.py
│   │   ├── deps.py
│   │   ├── schemas.py
│   │   ├── models/
│   │   │   └── account.py
│   │   ├── api/
│   │   │   ├── routes.py
│   │   │   └── rental.py
│   │   └── services/
│   │       ├── account_store.py
│   │       ├── telegram_manager.py
│   │       ├── agent_manager.py
│   │       ├── llm_engine.py
│   │       ├── model_catalog.py
│   │       ├── character_store.py
│   │       ├── rental_store.py
│   │       ├── worker_store.py
│   │       ├── host_metrics.py
│   │       ├── chat_memory.py
│   │       ├── conversation_director.py
│   │       ├── world_context.py
│   │       └── log_hub.py
│   ├── requirements.txt
│   ├── .env.example
│   ├── run.py
│   └── worker.py
├── frontend/
│   ├── index.html
│   ├── login.html
│   ├── css/
│   │   └── styles.css
│   └── js/
│       ├── api.js
│       ├── app.js
│       ├── accounts.js
│       ├── wizard.js
│       ├── settings.js
│       ├── tenants.js
│       └── console.js
├── remote-worker/
│   ├── server.py
│   ├── host_metrics.py
│   ├── ensure_llama.py
│   ├── requirements.txt
│   ├── start.bat
│   ├── start.sh
│   ├── run-server.ps1
│   ├── fetch-runtime.ps1
│   └── open-firewall.bat
├── data/
│   ├── models/
│   ├── rules/
│   ├── sessions/
│   ├── accounts.json
│   ├── characters.json
│   └── workers.json
└── Telegram/
    └── telegram_client.py
```
