"""Runtime configuration (environment variables with safe defaults)."""
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


COMPOSER_VERSION = "1.0.0"
DB_PATH = _env("VERA_DB_PATH", "vera_state.db")
SESSION_IDLE_RESET = _int("VERA_SESSION_IDLE_RESET", 45 * 60)   # seconds; 0 disables
MAX_BODY_BYTES = 600 * 1024

TEAM_NAME = _env("TEAM_NAME", "Vaibhav Tripathi")
TEAM_MEMBERS = [m.strip() for m in _env("TEAM_MEMBERS", "Vaibhav Tripathi").split(",") if m.strip()]
CONTACT_EMAIL = _env("CONTACT_EMAIL", "")
SUBMITTED_AT = _env("SUBMITTED_AT", "2026-09-27T00:00:00Z")

# LLM (all optional - bot is fully functional on templates alone)
LLM_ENABLED = _env("LLM_ENABLED", "1") == "1"
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_WRITER_MODEL = _env("GEMINI_WRITER_MODEL", "gemini-2.5-flash")
GEMINI_CRITIC_MODEL = _env("GEMINI_CRITIC_MODEL", "gemini-2.5-flash-lite")
GEMINI_WRITER_RPM = _int("GEMINI_WRITER_RPM", 8)
GEMINI_WRITER_RPD = _int("GEMINI_WRITER_RPD", 200)
GEMINI_CRITIC_RPM = _int("GEMINI_CRITIC_RPM", 12)
GEMINI_CRITIC_RPD = _int("GEMINI_CRITIC_RPD", 800)
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_RPM = _int("GROQ_RPM", 25)
GROQ_RPD = _int("GROQ_RPD", 800)
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", "6.5"))

TICK_DEADLINE = float(_env("TICK_DEADLINE", "7.5"))      # seconds of wall time per /tick
REPLY_DEADLINE = float(_env("REPLY_DEADLINE", "5.5"))
MAX_ACTIONS_PER_TICK = 20
MAX_MERCHANT_FACING_PER_MERCHANT_PER_TICK = 2
