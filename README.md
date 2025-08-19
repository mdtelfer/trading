    # Trading Macro-Fundamental Evaluator

    Sistema de **trading algorítmico** con enfoque *long-only*, que integra análisis fundamental y técnico para habilitar o bloquear operaciones, bajo reglas estrictas de riesgo y disciplina.

    ## 🚀 Objetivo
    - Evaluar automáticamente el contexto macro.
    - Determinar si se permite abrir nuevas posiciones (`long_permission`).
    - Ajustar el `risk_multiplier` según condiciones de mercado.
    - Generar journaling y alertas (watchdog).

    ## ⚙️ Arquitectura (alto nivel)
    ```mermaid
    flowchart LR
      A[TradingView
(señales técnicas)] --> B[Gateway Python
(webhooks → normalize)]
      B --> C[Risk Engine
(límites globales, DD, grupos)]
      C --> D[Macro Evaluator
(fast/slow → fused)]
      D --> E[MT5 Router
(ejecución)]
      D --> F[(Postgres: core.*)]
      F --> G[Dashboard
(Streamlit)]
      F --> H[Watchdog
(lags & evaluator)]
      H --> F
      H --> I[Alertas
(Telegram/Discord)]
    ```

    ## 🧩 Componentes

    ### Macro Evaluator
    - Reglas desde `configs/fundamental_rules.yaml`.
    - Corre en dos frecuencias: `fast` (5m), `slow` (1h). Inserta `fused`.
    - Tabla destino: `core.macro_state` (y vistas derivadas).

    ### Fuentes Fundamentales
    - FRED, yfinance, alternative.me, forexfactory (política actual: **IBKR no es fuente primaria** para features de contexto).
    - Los features controlan *filtros* y *riesgo*, no son triggers de entrada.

    ### Esquema de Base de Datos (Postgres)
    ```mermaid
    erDiagram
      core.macro_ticks {
        bigint id PK
        timestamptz ts
        text feature
        numeric value
        jsonb quality_flags
        boolean is_historic
        text source_type
        text ingest_source
        timestamptz ingested_at
        text status
      }
      core.macro_state {
        timestamptz ts
        text tier
        boolean long_permission
        numeric risk_multiplier
        text[] allowed_groups
        text[] prioritize
        text[] avoid
        text reason
        jsonb meta
      }
      core.alerts {
        bigserial id PK
        timestamptz created_at
        text kind
        text key
        bigint age_sec
        bigint threshold_sec
        jsonb payload
      }
      core.macro_ticks ||--o{ core.v_macro_latest : "VIEW"
      core.macro_state ||--o{ core.macro_state_latest : "VIEW"
      core.macro_state ||--o{ core.macro_state_fused_latest : "VIEW"
      core.macro_ticks ||--o{ core.cagg_macro_5m : "VIEW"
      core.macro_ticks ||--o{ core.cagg_macro_15m : "VIEW"
      core.macro_ticks ||--o{ core.cagg_macro_1h : "VIEW"
      core.macro_state ||--o{ core.v_evaluator_freshness : "VIEW"
      core.macro_ticks ||--o{ core.v_macro_dashboard : "VIEW"
    ```

    ### Watchdog
    - Monitorea:
      - Frescura de **features** (derivada de `v_macro_latest`).
      - Frescura del **evaluator** (`v_evaluator_freshness`).
    - Persiste en `core.alerts` y puede enviar a Telegram/Discord.
    - **Umbrales** configurables por YAML (`configs/freshness_policies.yaml`).

    ## 🧪 Workflow
    1) Ingesta → `macro_ticks` (runners externos y MT5).
    2) Evaluación → `macro_state` (`fast/slow/fused`).
    3) Watchdog → `core.alerts` + logs.
    4) Panel → `v_macro_dashboard` / Streamlit.

    ## 📦 Archivos clave

    - `scripts/watchdog.py`
      Watchdog con soporte de políticas por YAML.
    - `configs/freshness_policies.yaml`
      Umbrales por feature/tier (override) + supresión.
    - `migrations/20250819_watchdog_patch.sql`
      Patch idempotente para contenedor (schema, tabla, índices, vistas watchdog).

    ## ▶️ Ejecución rápida

    **Watchdog** (PowerShell):
    ```powershell
    $env:DB_HOST="localhost"; $env:DB_PORT="5432"; $env:DB_NAME="trading"; $env:DB_USER="postgres"; $env:DB_PASSWORD="postgres"
    $env:WATCHDOG_EVERY_SEC="60"; $env:FEATURE_THRESHOLD_SEC="900"
    $env:FRESHNESS_POLICIES="configs/freshness_policies.yaml"
    python scripts/watchdog.py
    ```

    **Aplicar patch en contenedor**:
    ```bash
    docker cp migrations/20250819_watchdog_patch.sql <DB_CONTAINER>:/tmp/patch.sql
    docker exec -e PGPASSWORD=$POSTGRES_PASSWORD -it <DB_CONTAINER>       psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -f /tmp/patch.sql
    ```

    ## ✅ Estado actual
    - Ingesta estable (externa + MT5).
    - Evaluador operando (fast/slow/fused).
    - Watchdog corriendo y persistiendo en `core.alerts`.
    - Política de fuentes: macro/contexto en FRED/yfinance; **IBKR reservado** para técnico/ejecución.

    ## 📅 Próximos pasos
    - Webhooks de alertas (Telegram/Discord).
    - Dashboard de frescura por feature.
    - Documentar `fundamental_rules.yaml` y `fundamental_sources.yaml`.
    - Backtesting técnico (separado del macro).
    ```
