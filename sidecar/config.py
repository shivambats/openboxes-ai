"""Configuration is read from the process environment; never commit credentials."""
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    openboxes_url: str = field(default_factory=lambda: os.getenv('OPENBOXES_URL', 'http://localhost/openboxes').rstrip('/'))
    username: str = field(default_factory=lambda: os.getenv('OPENBOXES_USERNAME', 'sidecar_bot'))
    password: str = field(default_factory=lambda: os.getenv('OPENBOXES_PASSWORD', ''))
    location_id: str = field(default_factory=lambda: os.getenv('OPENBOXES_LOCATION_ID', ''))
    api_key: str = field(default_factory=lambda: os.getenv('OPENBOXES_API_KEY', ''))
    api_key_header: str = field(default_factory=lambda: os.getenv('OPENBOXES_API_KEY_HEADER', 'X-API-Key'))
    easypost_key: str = field(default_factory=lambda: os.getenv('EASYPOST_API_KEY', ''))
    shippo_key: str = field(default_factory=lambda: os.getenv('SHIPPO_API_KEY', ''))
    shippo_carrier: str = field(default_factory=lambda: os.getenv('SHIPPO_CARRIER', 'shippo'))
    browser_path: str = field(default_factory=lambda: os.getenv('BROWSER_EXECUTABLE_PATH', ''))
    cdp_url: str = field(default_factory=lambda: os.getenv('BROWSER_CDP_URL', ''))
    portal_origins: tuple = field(default_factory=lambda: tuple(os.getenv('PORTAL_ALLOWED_ORIGINS', 'http://localhost:8080').split(',')))
    context_mode: str = field(default_factory=lambda: os.getenv('CONTEXT_MODE', 'llm'))
    llm_url: str = field(default_factory=lambda: os.getenv('LLM_CHAT_URL', ''))
    llm_key: str = field(default_factory=lambda: os.getenv('LLM_API_KEY', ''))
    llm_model: str = field(default_factory=lambda: os.getenv('LLM_MODEL', ''))
    fixtures: bool = field(default_factory=lambda: os.getenv('CARRIER_FIXTURES', 'false').lower() == 'true')
    state_path: str = field(default_factory=lambda: os.getenv('SIDECAR_STATE_PATH', 'sidecar.sqlite3'))
    agentmail_key: str = field(default_factory=lambda: os.getenv('AGENTMAIL_API_KEY', ''))
    agentmail_inbox: str = field(default_factory=lambda: os.getenv('AGENTMAIL_INBOX', 'dunder-mifflin-paper-co-shipment@agentmail.to'))
    openrouter_key: str = field(default_factory=lambda: os.getenv('OPENROUTER_API_KEY') or os.getenv('OPENROUTER_KEY', ''))
    openrouter_model: str = field(default_factory=lambda: os.getenv('OPENROUTER_MODEL', 'deepseek/deepseek-v4.1-flash'))
    image_model: str = field(default_factory=lambda: os.getenv('IMAGE_MODEL', 'openai/gpt-4.1-mini'))
