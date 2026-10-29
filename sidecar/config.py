"""Configuration is read from the process environment; never commit credentials."""
import os
from dataclasses import dataclass, field


def _hours(name, default):
    """A positive number of hours; a typo must never switch a send guard off."""
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{name} must be a number of hours') from None
    if not 0 < value <= 24 * 30:
        raise ValueError(f'{name} must be more than 0 and at most 720 hours')
    return value


def _count(name, default):
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    if not raw.isdecimal() or not 0 <= int(raw) <= 10:
        raise ValueError(f'{name} must be a whole number from 0 to 10')
    return int(raw)


def _minutes(name, default):
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f'{name} must be a number of minutes') from None
    if not 1 <= value <= 24 * 60:
        raise ValueError(f'{name} must be from 1 to 1440 minutes')
    return value


@dataclass
class Config:
    openboxes_url: str = field(default_factory=lambda: os.getenv('OPENBOXES_URL', 'http://localhost/openboxes').rstrip('/'))
    username: str = field(default_factory=lambda: os.getenv('OPENBOXES_USERNAME', 'vegath-agent'))
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
    tow_research_model: str = field(default_factory=lambda: os.getenv(
        'TOW_RESEARCH_MODEL', 'openai/gpt-4.1-mini'))
    openboxes_sidecar_token: str = field(default_factory=lambda: os.getenv('OPENBOXES_SIDECAR_TOKEN', ''))
    # Wait before asking an unanswered shipment contact or recovery carrier again.
    contact_follow_up_hours: float = field(default_factory=lambda: _hours(
        'CONTACT_FOLLOW_UP_HOURS', 4.0))
    carrier_follow_up_hours: float = field(default_factory=lambda: _hours(
        'CARRIER_FOLLOW_UP_HOURS', 2.0))
    # Background chase of unanswered contacts on late or at-risk shipments.
    contact_auto_chase: bool = field(default_factory=lambda: os.getenv(
        'CONTACT_AUTO_CHASE', 'true').strip().lower() in {'1', 'true', 'yes'})
    contact_auto_follow_up_limit: int = field(default_factory=lambda: _count(
        'CONTACT_AUTO_FOLLOW_UP_LIMIT', 2))
    contact_monitor_interval_minutes: float = field(default_factory=lambda: _minutes(
        'CONTACT_MONITOR_INTERVAL_MINUTES', 10.0))
