from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(default="sqlite:///radio.db", alias="DATABASE_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    llm_model: str = Field(default="gpt-4.1-mini", alias="LLM_MODEL")
    xai_api_key: str | None = Field(default=None, alias="XAI_API_KEY")
    xai_base_url: str = Field(default="https://api.x.ai/v1", alias="XAI_BASE_URL")
    xai_model: str = Field(default="grok-4-1-fast-non-reasoning", alias="XAI_MODEL")
    xai_search_model: str = Field(default="grok-4-1-fast-non-reasoning", alias="XAI_SEARCH_MODEL")
    enable_grok_search: bool = Field(default=False, alias="ENABLE_GROK_SEARCH")
    max_grok_calls_per_day: int = Field(default=60, alias="MAX_GROK_CALLS_PER_DAY")
    max_grok_calls_per_month: int = Field(default=1800, alias="MAX_GROK_CALLS_PER_MONTH")
    max_grok_usd_per_month: float = Field(default=5.0, alias="MAX_GROK_USD_PER_MONTH")
    grok_estimated_cost_per_call_usd: float = Field(default=0.005, alias="GROK_ESTIMATED_COST_PER_CALL_USD")
    grok_enable_detail_query: bool = Field(default=True, alias="GROK_ENABLE_DETAIL_QUERY")
    grok_detail_query_suffix: str = Field(
        default='submit music OR playlist OR "program director" OR dj OR host',
        alias="GROK_DETAIL_QUERY_SUFFIX",
    )
    llm_provider_mode: str = Field(default="openai", alias="LLM_PROVIDER_MODE")
    llm_hybrid_xai_percent: int = Field(default=30, alias="LLM_HYBRID_XAI_PERCENT")

    brave_api_key: str | None = Field(default=None, alias="BRAVE_API_KEY")
    brave_base_url: str = Field(default="https://api.search.brave.com/res/v1/web/search", alias="BRAVE_BASE_URL")
    max_brave_calls_per_month: int = Field(default=500, alias="MAX_BRAVE_CALLS_PER_MONTH")
    enable_brave_search: bool = Field(default=True, alias="ENABLE_BRAVE_SEARCH")
    brave_answer_api_key: str | None = Field(default=None, alias="BRAVE_ANSWER_API_KEY")
    brave_answer_base_url: str = Field(
        default="https://api.search.brave.com/res/v1/answer/search",
        alias="BRAVE_ANSWER_BASE_URL",
    )
    enable_brave_answer: bool = Field(default=False, alias="ENABLE_BRAVE_ANSWER")
    max_brave_answer_calls_per_day: int = Field(default=40, alias="MAX_BRAVE_ANSWER_CALLS_PER_DAY")
    max_brave_answer_calls_per_month: int = Field(default=1200, alias="MAX_BRAVE_ANSWER_CALLS_PER_MONTH")
    brave_answer_max_results_per_query: int = Field(default=5, alias="BRAVE_ANSWER_MAX_RESULTS_PER_QUERY")
    google_cse_api_key: str | None = Field(default=None, alias="GOOGLE_CSE_API_KEY")
    google_cse_cx: str | None = Field(default=None, alias="GOOGLE_CSE_CX")
    google_cse_base_url: str = Field(default="https://www.googleapis.com/customsearch/v1", alias="GOOGLE_CSE_BASE_URL")
    max_google_cse_calls_per_day: int = Field(default=100, alias="MAX_GOOGLE_CSE_CALLS_PER_DAY")
    max_google_cse_calls_per_month: int = Field(default=3000, alias="MAX_GOOGLE_CSE_CALLS_PER_MONTH")
    enable_google_cse_search: bool = Field(default=False, alias="ENABLE_GOOGLE_CSE_SEARCH")
    google_enable_detail_query: bool = Field(default=True, alias="GOOGLE_ENABLE_DETAIL_QUERY")
    google_detail_query_suffix: str = Field(
        default='submit music OR playlist OR "program director" OR dj OR host',
        alias="GOOGLE_DETAIL_QUERY_SUFFIX",
    )
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")
    tavily_api_keys: str | None = Field(default=None, alias="TAVILY_API_KEYS")
    tavily_base_url: str = Field(default="https://api.tavily.com/search", alias="TAVILY_BASE_URL")
    max_tavily_calls_per_day: int = Field(default=100, alias="MAX_TAVILY_CALLS_PER_DAY")
    max_tavily_calls_per_month: int = Field(default=1000, alias="MAX_TAVILY_CALLS_PER_MONTH")
    enable_tavily_search: bool = Field(default=False, alias="ENABLE_TAVILY_SEARCH")
    tavily_enable_detail_query: bool = Field(default=True, alias="TAVILY_ENABLE_DETAIL_QUERY")
    tavily_detail_query_suffix: str = Field(
        default='submit music OR playlist OR "program director" OR dj OR host',
        alias="TAVILY_DETAIL_QUERY_SUFFIX",
    )
    linkup_api_key: str | None = Field(default=None, alias="LINKUP_API_KEY")
    linkup_api_keys: str | None = Field(default=None, alias="LINKUP_API_KEYS")
    linkup_base_url: str = Field(default="https://api.linkup.so/v1/search", alias="LINKUP_BASE_URL")
    max_linkup_calls_per_day: int = Field(default=100, alias="MAX_LINKUP_CALLS_PER_DAY")
    max_linkup_calls_per_month: int = Field(default=1000, alias="MAX_LINKUP_CALLS_PER_MONTH")
    enable_linkup_search: bool = Field(default=False, alias="ENABLE_LINKUP_SEARCH")
    linkup_enable_detail_query: bool = Field(default=True, alias="LINKUP_ENABLE_DETAIL_QUERY")
    linkup_detail_query_suffix: str = Field(
        default='submit music OR playlist OR "program director" OR dj OR host',
        alias="LINKUP_DETAIL_QUERY_SUFFIX",
    )
    max_duckduckgo_calls_per_day: int = Field(default=300, alias="MAX_DUCKDUCKGO_CALLS_PER_DAY")
    max_duckduckgo_calls_per_month: int = Field(default=9000, alias="MAX_DUCKDUCKGO_CALLS_PER_MONTH")
    enable_duckduckgo_search: bool = Field(default=False, alias="ENABLE_DUCKDUCKGO_SEARCH")
    duckduckgo_enable_detail_query: bool = Field(default=False, alias="DUCKDUCKGO_ENABLE_DETAIL_QUERY")
    duckduckgo_detail_query_suffix: str = Field(
        default='submit music OR playlist OR "program director" OR dj OR host',
        alias="DUCKDUCKGO_DETAIL_QUERY_SUFFIX",
    )
    max_results_per_source: int = Field(default=10, alias="MAX_RESULTS_PER_SOURCE")
    discovery_create_new_stations: bool = Field(default=False, alias="DISCOVERY_CREATE_NEW_STATIONS")

    radio_browser_base_url: str = Field(default="https://de1.api.radio-browser.info/json", alias="RADIO_BROWSER_BASE_URL")
    wikidata_sparql_url: str = Field(default="https://query.wikidata.org/sparql", alias="WIKIDATA_SPARQL_URL")

    crawl_user_agent: str = Field(default="RadioDBBot/0.1 (+research)", alias="CRAWL_USER_AGENT")
    max_search_results: int = Field(default=20, alias="MAX_SEARCH_RESULTS")
    batch_size: int = Field(default=25, alias="BATCH_SIZE")
    max_page_fetches_per_run: int = Field(default=60, alias="MAX_PAGE_FETCHES_PER_RUN")
    max_llm_calls_per_run: int = Field(default=25, alias="MAX_LLM_CALLS_PER_RUN")
    max_llm_calls_per_domain_per_run: int = Field(default=2, alias="MAX_LLM_CALLS_PER_DOMAIN_PER_RUN")
    max_llm_calls_per_day: int = Field(default=400, alias="MAX_LLM_CALLS_PER_DAY")
    max_daily_usd: float = Field(default=3.0, alias="MAX_DAILY_USD")
    llm_input_price_per_1m: float = Field(default=0.40, alias="LLM_INPUT_PRICE_PER_1M")
    llm_output_price_per_1m: float = Field(default=1.60, alias="LLM_OUTPUT_PRICE_PER_1M")
    openai_input_price_per_1m: float = Field(default=0.40, alias="OPENAI_INPUT_PRICE_PER_1M")
    openai_output_price_per_1m: float = Field(default=1.60, alias="OPENAI_OUTPUT_PRICE_PER_1M")
    xai_input_price_per_1m: float = Field(default=0.20, alias="XAI_INPUT_PRICE_PER_1M")
    xai_output_price_per_1m: float = Field(default=0.50, alias="XAI_OUTPUT_PRICE_PER_1M")
    llm_estimated_output_tokens: int = Field(default=450, alias="LLM_ESTIMATED_OUTPUT_TOKENS")
    llm_max_prompt_chars: int = Field(default=8000, alias="LLM_MAX_PROMPT_CHARS")
    profile_max_evidence_items: int = Field(default=25, alias="PROFILE_MAX_EVIDENCE_ITEMS")
    people_min_station_confidence: float = Field(default=0.55, alias="PEOPLE_MIN_STATION_CONFIDENCE")
    people_discovery_min_station_confidence: float = Field(default=0.65, alias="PEOPLE_DISCOVERY_MIN_STATION_CONFIDENCE")
    people_min_confidence: float = Field(default=0.45, alias="PEOPLE_MIN_CONFIDENCE")
    people_require_known_role: bool = Field(default=True, alias="PEOPLE_REQUIRE_KNOWN_ROLE")
    people_require_name_or_email: bool = Field(default=True, alias="PEOPLE_REQUIRE_NAME_OR_EMAIL")
    people_allow_unknown_role_with_linkedin: bool = Field(default=False, alias="PEOPLE_ALLOW_UNKNOWN_ROLE_WITH_LINKEDIN")
    station_enrich_min_station_confidence: float = Field(default=0.6, alias="STATION_ENRICH_MIN_STATION_CONFIDENCE")
    station_enrich_max_results_per_query: int = Field(default=8, alias="STATION_ENRICH_MAX_RESULTS_PER_QUERY")
    station_enrich_max_page_fetches: int = Field(default=40, alias="STATION_ENRICH_MAX_PAGE_FETCHES")
    station_enrich_use_google: bool = Field(default=False, alias="STATION_ENRICH_USE_GOOGLE")
    station_enrich_use_tavily: bool = Field(default=True, alias="STATION_ENRICH_USE_TAVILY")
    station_enrich_use_grok: bool = Field(default=True, alias="STATION_ENRICH_USE_GROK")
    station_enrich_use_linkup: bool = Field(default=False, alias="STATION_ENRICH_USE_LINKUP")
    station_enrich_use_duckduckgo: bool = Field(default=False, alias="STATION_ENRICH_USE_DUCKDUCKGO")
    station_enrich_use_sitemap: bool = Field(default=True, alias="STATION_ENRICH_USE_SITEMAP")
    station_enrich_use_rss: bool = Field(default=True, alias="STATION_ENRICH_USE_RSS")
    station_enrich_max_internal_urls_per_station: int = Field(default=8, alias="STATION_ENRICH_MAX_INTERNAL_URLS_PER_STATION")
    station_enrich_high_priority_share: float = Field(default=0.7, alias="STATION_ENRICH_HIGH_PRIORITY_SHARE")
    station_enrich_maintenance_share: float = Field(default=0.2, alias="STATION_ENRICH_MAINTENANCE_SHARE")
    station_enrich_exploration_share: float = Field(default=0.1, alias="STATION_ENRICH_EXPLORATION_SHARE")
    station_enrich_maintenance_days_overdue: int = Field(default=60, alias="STATION_ENRICH_MAINTENANCE_DAYS_OVERDUE")
    station_enrich_pool_multiplier: int = Field(default=6, alias="STATION_ENRICH_POOL_MULTIPLIER")
    station_pitch_ready_min_score: int = Field(default=60, alias="STATION_PITCH_READY_MIN_SCORE")
    station_enrich_cooldown_hours: int = Field(default=24, alias="STATION_ENRICH_COOLDOWN_HOURS")
    brave_boost_default_budget_usd: float = Field(default=3.0, alias="BRAVE_BOOST_DEFAULT_BUDGET_USD")
    brave_boost_usd_per_call: float = Field(default=0.015, alias="BRAVE_BOOST_USD_PER_CALL")
    brave_boost_no_repeat_days: int = Field(default=14, alias="BRAVE_BOOST_NO_REPEAT_DAYS")
    brave_boost_min_station_confidence: float = Field(default=0.6, alias="BRAVE_BOOST_MIN_STATION_CONFIDENCE")
    high_priority_deep_dive_station_limit: int = Field(default=20, alias="HIGH_PRIORITY_DEEP_DIVE_STATION_LIMIT")
    people_discovery_brave_answer_max_stations_per_run: int = Field(
        default=60,
        alias="PEOPLE_DISCOVERY_BRAVE_ANSWER_MAX_STATIONS_PER_RUN",
    )
    evidence_dedupe_hours: int = Field(default=24, alias="EVIDENCE_DEDUPE_HOURS")
    browser_worker_enabled: bool = Field(default=True, alias="BROWSER_WORKER_ENABLED")
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_timeout_ms: int = Field(default=20000, alias="BROWSER_TIMEOUT_MS")
    browser_proxy_url: str = Field(default="", alias="BROWSER_PROXY_URL")
    browser_max_stations_per_run: int = Field(default=50, alias="BROWSER_MAX_STATIONS_PER_RUN")
    browser_max_forms_per_station: int = Field(default=5, alias="BROWSER_MAX_FORMS_PER_STATION")
    browser_snapshot_dir: str = Field(default=".radio_db_state/form_snapshots", alias="BROWSER_SNAPSHOT_DIR")
    browser_enable_deep_pass: bool = Field(default=True, alias="BROWSER_ENABLE_DEEP_PASS")
    browser_deep_max_urls_per_station: int = Field(default=10, alias="BROWSER_DEEP_MAX_URLS_PER_STATION")
    submission_exception_registry_path: str = Field(
        default="config/submission_exceptions.json",
        alias="SUBMISSION_EXCEPTION_REGISTRY_PATH",
    )
    submission_excluded_focus_keywords: str = Field(
        default="jazz",
        alias="SUBMISSION_EXCLUDED_FOCUS_KEYWORDS",
    )
    priority_countries: str = Field(
        default="US,GB,DE,FR,CA,AU,NL,SE,NO,ES,IT,AT,CH,BE,IE,JP",
        alias="PRIORITY_COUNTRIES",
    )
    codex_recipe_model: str = Field(default="gpt-4.1-mini", alias="CODEX_RECIPE_MODEL")
    enable_codex_recipe_enrichment: bool = Field(default=True, alias="ENABLE_CODEX_RECIPE_ENRICHMENT")
    max_codex_calls_per_day: int = Field(default=120, alias="MAX_CODEX_CALLS_PER_DAY")
    max_codex_daily_usd: float = Field(default=5.0, alias="MAX_CODEX_DAILY_USD")
    codex_input_price_per_1m: float = Field(default=0.4, alias="CODEX_INPUT_PRICE_PER_1M")
    codex_output_price_per_1m: float = Field(default=1.6, alias="CODEX_OUTPUT_PRICE_PER_1M")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_base_url: str | None = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        alias="GEMINI_BASE_URL",
    )
    station_quality_provider: str = Field(default="gemini", alias="STATION_QUALITY_PROVIDER")
    station_quality_assessment_kind: str = Field(default="llm_quality_v1", alias="STATION_QUALITY_ASSESSMENT_KIND")
    station_quality_openai_model: str = Field(default="gpt-5.4-mini", alias="STATION_QUALITY_OPENAI_MODEL")
    station_quality_gemini_model: str = Field(default="gemini-2.5-flash-lite", alias="STATION_QUALITY_GEMINI_MODEL")
    station_quality_estimated_output_tokens: int = Field(
        default=220,
        alias="STATION_QUALITY_ESTIMATED_OUTPUT_TOKENS",
    )
    station_quality_gemini_input_price_per_1m: float = Field(
        default=0.10,
        alias="STATION_QUALITY_GEMINI_INPUT_PRICE_PER_1M",
    )
    station_quality_gemini_output_price_per_1m: float = Field(
        default=0.40,
        alias="STATION_QUALITY_GEMINI_OUTPUT_PRICE_PER_1M",
    )
    station_quality_max_calls_per_day: int = Field(default=1200, alias="STATION_QUALITY_MAX_CALLS_PER_DAY")
    station_quality_max_daily_usd: float = Field(default=5.0, alias="STATION_QUALITY_MAX_DAILY_USD")
    station_quality_gemini_max_output_tokens: int = Field(
        default=400,
        alias="STATION_QUALITY_GEMINI_MAX_OUTPUT_TOKENS",
    )
    station_quality_exception_registry_path: str = Field(
        default="config/station_quality_exceptions.json",
        alias="STATION_QUALITY_EXCEPTION_REGISTRY_PATH",
    )
    station_quality_deep_max_pages: int = Field(default=6, alias="STATION_QUALITY_DEEP_MAX_PAGES")
    station_quality_deep_fetch_timeout_seconds: int = Field(
        default=15,
        alias="STATION_QUALITY_DEEP_FETCH_TIMEOUT_SECONDS",
    )
    rejected_scan_main_database_url: str = Field(
        default="sqlite:////opt/radio-database/radio.db",
        alias="REJECTED_SCAN_MAIN_DATABASE_URL",
    )
    country_discovery_max_queries_per_run: int = Field(default=8, alias="COUNTRY_DISCOVERY_MAX_QUERIES_PER_RUN")
    country_discovery_min_confidence: float = Field(default=0.35, alias="COUNTRY_DISCOVERY_MIN_CONFIDENCE")
    country_discovery_include_linkup: bool = Field(default=True, alias="COUNTRY_DISCOVERY_INCLUDE_LINKUP")
    country_discovery_max_results_per_query: int = Field(default=15, alias="COUNTRY_DISCOVERY_MAX_RESULTS_PER_QUERY")


settings = Settings()
