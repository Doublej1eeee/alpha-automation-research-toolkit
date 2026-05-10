import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

import requests
import yaml
from requests.auth import HTTPBasicAuth


BASE_URL = "https://api.worldquantbrain.com"
RESULT_SCHEMA_VERSION = 2

DEFAULT_CREDENTIALS_FILE = Path("brain_credentials.json")
CREDENTIALS_FILE = Path(os.getenv("BRAIN_CREDENTIALS_FILE", str(DEFAULT_CREDENTIALS_FILE)))
RESULTS_DIR = Path("result_store")
RESULTS_RAW_DIR = RESULTS_DIR / "raw"
RESULTS_BATCHES_DIR = RESULTS_DIR / "batches"
RESULTS_INDEX_DIR = RESULTS_DIR / "index"
RESULTS_INDEX_FINGERPRINTS_FILE = RESULTS_INDEX_DIR / "fingerprints.jsonl"
RESULTS_INDEX_CATALOG_FILE = RESULTS_INDEX_DIR / "alpha_catalog.jsonl"
RESULTS_INDEX_TEMPLATE_FIELD_FILE = RESULTS_INDEX_DIR / "template_field_catalog.jsonl"
EXPERIENCE_DIR = RESULTS_DIR / "analysis"
ALPHA_TRUTH_STATE_FILE = EXPERIENCE_DIR / "alpha_truth_state.json"
EXPERIENCE_LOGS_DIR = EXPERIENCE_DIR / "logs"
EXPERIENCE_FAILURES_DIR = EXPERIENCE_LOGS_DIR
EXPERIENCE_PATTERNS_DIR = RESULTS_DIR / "high_quality"
EXPERIENCE_PENDING_DIR = EXPERIENCE_LOGS_DIR
EXPERIENCE_AVERAGE_DIR = EXPERIENCE_LOGS_DIR
EXPERIENCE_GOOD_DIR = EXPERIENCE_LOGS_DIR
EXPERIENCE_EXCELLENT_DIR = EXPERIENCE_PATTERNS_DIR / "excellent"
EXPERIENCE_SPECTACULAR_DIR = EXPERIENCE_PATTERNS_DIR / "spectacular"
RATE_LIMIT_EVENTS_FILE = EXPERIENCE_LOGS_DIR / "rate_limit_events.jsonl"

CHECK_PREVIEW_MAX_RETRIES = 3
CHECK_PREVIEW_RETRY_DELAY_SECONDS = 8
AUTH_MAX_RETRIES = 5
AUTH_RETRY_DELAY_SECONDS = 10
SIMULATION_SUBMIT_MAX_RETRIES = 12
SIMULATION_SUBMIT_RETRY_SECONDS = 20
SIMULATION_SUBMIT_MAX_BACKOFF_SECONDS = 180
SIMULATION_WAIT_MAX_SECONDS = int(os.getenv("BRAIN_SIMULATION_WAIT_MAX_SECONDS", "1800"))
SIMULATION_STALLED_MAX_SECONDS = int(os.getenv("BRAIN_SIMULATION_STALLED_MAX_SECONDS", "900"))


COLOR_MAP = {
    "red": "RED",
    "yellow": "YELLOW",
    "green": "GREEN",
    "blue": "BLUE",
    "purple": "PURPLE",
}

GRADE_TO_COLOR = {
    "INFERIOR": "RED",
    "AVERAGE": "YELLOW",
    "GOOD": "GREEN",
    "EXCELLENT": "BLUE",
    "SPECTACULAR": "PURPLE",
}

COLOR_TO_GRADE = {value: key for key, value in GRADE_TO_COLOR.items()}
GRADE_TAG_PREFIX = "1"
REPAIR_TAG = "1REPAIR"
REPAIR_FAMILY_TAG_PREFIX = "FAM_"
SUBMIT_PASS_GATE_COUNT = 8
BASE_PASS_GATE_COUNT = 7
CORRELATION_CHECK_NAMES = {"SELF_CORRELATION"}


CATEGORY_MAP = {
    "price_reversion": "PRICE_REVERSION",
    "price reversion": "PRICE_REVERSION",
    "price_momentum": "PRICE_MOMENTUM",
    "price momentum": "PRICE_MOMENTUM",
    "volume": "VOLUME",
    "fundamental": "FUNDAMENTAL",
    "analyst": "ANALYST",
    "price_volume": "PRICE_VOLUME",
    "price volume": "PRICE_VOLUME",
    "relation": "RELATION",
    "sentiment": "SENTIMENT",
}


EXPERIENCE_NOTE_DIRS = [
    EXPERIENCE_FAILURES_DIR,
    EXPERIENCE_PENDING_DIR,
    EXPERIENCE_PATTERNS_DIR,
    EXPERIENCE_AVERAGE_DIR,
    EXPERIENCE_GOOD_DIR,
    EXPERIENCE_EXCELLENT_DIR,
    EXPERIENCE_SPECTACULAR_DIR,
]

IMPORTANT_RESULT_COLORS = {"YELLOW", "GREEN", "BLUE", "PURPLE"}
IMPORTANT_EXPERIENCE_COLORS = {"BLUE", "PURPLE"}


def ensure_local_state_directories() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_RAW_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    EXPERIENCE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    for directory in EXPERIENCE_NOTE_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def log_rate_limit_event(
    kind: str,
    status_code: int = 429,
    retry_seconds: float | int | None = None,
    attempt: int | None = None,
    context: dict | None = None,
) -> None:
    ensure_local_state_directories()
    append_jsonl(
        RATE_LIMIT_EVENTS_FILE,
        {
            "ts": utc_now(),
            "kind": str(kind),
            "status_code": int(status_code),
            "retry_seconds": retry_seconds,
            "attempt": attempt,
            "context": context or {},
        },
    )


def _credential_candidate_paths(path: Path) -> list[Path]:
    home = Path.home()
    candidates = [path]
    env_path = os.getenv("BRAIN_CREDENTIALS_FILE")
    if env_path:
        candidates.insert(0, Path(env_path))
    candidates.extend(
        [
            home / ".learning" / "brain_credentials.json",
            home / ".config" / "learning" / "brain_credentials.json",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def load_credentials(path: Path) -> tuple[str, str]:
    env_username = os.getenv("BRAIN_USERNAME")
    env_password = os.getenv("BRAIN_PASSWORD")
    if env_username and env_password:
        return env_username, env_password

    resolved_path = None
    for candidate in _credential_candidate_paths(path):
        if candidate.exists():
            resolved_path = candidate
            break

    if resolved_path is None:
        raise FileNotFoundError(
            f"Credentials file not found. Checked: {', '.join(str(item) for item in _credential_candidate_paths(path))}\n"
            "Set BRAIN_USERNAME/BRAIN_PASSWORD or BRAIN_CREDENTIALS_FILE, "
            "or create brain_credentials.json first."
        )

    with resolved_path.open("r", encoding="utf-8") as file:
        credentials = json.load(file)

    if not isinstance(credentials, list) or len(credentials) != 2:
        raise ValueError(
            f"{resolved_path.name} must look like:\n"
            '["your_email@example.com", "your_password"]'
        )

    username, password = credentials
    return username, password


def login(username: str, password: str) -> requests.Session:
    session = requests.Session()
    session.auth = HTTPBasicAuth(username, password)

    for attempt in range(1, AUTH_MAX_RETRIES + 1):
        response = session.post(f"{BASE_URL}/authentication")
        print("Login status:", response.status_code)

        if response.status_code in (200, 201):
            try:
                print("Login response:", response.json())
            except Exception:
                print("Login response:", response.text)
            return session

        if response.status_code == 429 and attempt < AUTH_MAX_RETRIES:
            log_rate_limit_event(
                kind="login",
                status_code=response.status_code,
                retry_seconds=AUTH_RETRY_DELAY_SECONDS,
                attempt=attempt,
            )
            print(
                f"Login rate-limited. Retry {attempt}/{AUTH_MAX_RETRIES - 1} "
                f"after {AUTH_RETRY_DELAY_SECONDS}s."
            )
            sleep(AUTH_RETRY_DELAY_SECONDS)
            continue

        raise RuntimeError(f"Login failed: {response.text}")

    raise RuntimeError("Login failed after retries.")


def build_default_settings() -> dict:
    return {
        "instrumentType": "EQUITY",
        "region": "USA",
        "universe": "TOP3000",
        "delay": 1,
        "decay": 0,
        "neutralization": "INDUSTRY",
        "truncation": 0.08,
        "pasteurization": "ON",
        "unitHandling": "VERIFY",
        "nanHandling": "OFF",
        "testPeriod": "P1Y",
        "language": "FASTEXPR",
        "visualization": False,
    }


def normalize_setting_value(key: str, value):
    if key in {"pasteurization", "nanHandling"}:
        if value is True:
            return "ON"
        if value is False:
            return "OFF"
    return value


def normalize_color(value: str | None) -> str | None:
    if not value:
        return None
    return COLOR_MAP.get(str(value).strip().lower())


def normalize_category(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw in {
        "PRICE_REVERSION",
        "PRICE_MOMENTUM",
        "VOLUME",
        "FUNDAMENTAL",
        "ANALYST",
        "PRICE_VOLUME",
        "RELATION",
        "SENTIMENT",
    }:
        return raw
    return CATEGORY_MAP.get(raw.lower())


def load_alpha_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Alpha file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a valid YAML mapping.")
    if "expression" not in data:
        raise ValueError(f"{path} is missing the expression field.")

    return data


def validate_alpha_config(config: dict, source_name: str = "config") -> dict:
    if not isinstance(config, dict):
        raise ValueError(f"{source_name} is not a valid alpha mapping.")
    if "expression" not in config:
        raise ValueError(f"{source_name} is missing the expression field.")
    return config


def build_simulation_data_from_config(config: dict) -> dict:
    simulation_type = config.get("type", "REGULAR")
    expression = config["expression"]

    settings = build_default_settings()
    raw_settings = config.get("settings", {})
    settings.update(
        {
            key: normalize_setting_value(key, value)
            for key, value in raw_settings.items()
        }
    )

    simulation_data = {"type": simulation_type, "settings": settings}
    if simulation_type != "REGULAR":
        raise ValueError(f"Unsupported simulation type: {simulation_type}")

    simulation_data["regular"] = expression
    return simulation_data


def build_alpha_fingerprint(config: dict) -> str:
    simulation_data = build_simulation_data_from_config(config)
    payload = {
        "type": simulation_data["type"],
        "settings": simulation_data["settings"],
        "expression": simulation_data["regular"],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def submit_simulation(session: requests.Session, simulation_data: dict) -> str:
    for attempt in range(1, SIMULATION_SUBMIT_MAX_RETRIES + 1):
        response = session.post(f"{BASE_URL}/simulations", json=simulation_data)
        print("Submit status:", response.status_code)

        if response.status_code in (200, 201):
            progress_url = response.headers.get("Location")
            if not progress_url:
                raise RuntimeError("Simulation response did not include Location header.")
            print("Progress URL:", progress_url)
            return progress_url

        if response.status_code == 429 and attempt < SIMULATION_SUBMIT_MAX_RETRIES:
            retry_after_header = response.headers.get("Retry-After")
            try:
                retry_after = float(retry_after_header) if retry_after_header is not None else 0.0
            except Exception:
                retry_after = 0.0
            backoff_seconds = min(
                max(
                    SIMULATION_SUBMIT_RETRY_SECONDS * attempt,
                    int(retry_after) if retry_after > 0 else 0,
                ),
                SIMULATION_SUBMIT_MAX_BACKOFF_SECONDS,
            )
            log_rate_limit_event(
                kind="submit_simulation",
                status_code=response.status_code,
                retry_seconds=backoff_seconds,
                attempt=attempt,
                context={
                    "retry_after_header": retry_after_header,
                    "expression_preview": str((simulation_data or {}).get("regular") or "")[:200],
                    "region": ((simulation_data or {}).get("settings") or {}).get("region"),
                    "universe": ((simulation_data or {}).get("settings") or {}).get("universe"),
                    "delay": ((simulation_data or {}).get("settings") or {}).get("delay"),
                },
            )
            print(
                "Submit rate-limited or concurrent limit reached. "
                f"Retry {attempt}/{SIMULATION_SUBMIT_MAX_RETRIES - 1} "
                f"after {backoff_seconds}s."
            )
            sleep(backoff_seconds)
            continue

        raise RuntimeError(f"Submit failed: {response.text}")

    raise RuntimeError("Submit failed after retries.")


def wait_for_simulation(session: requests.Session, progress_url: str) -> dict:
    started_at = monotonic()
    last_progress_text = None
    last_progress_change_at = started_at
    while True:
        response = session.get(progress_url)
        retry_after = float(response.headers.get("Retry-After", 0))
        progress_text = "unknown"

        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict) and "progress" in payload:
            try:
                progress_value = float(payload["progress"])
                progress_text = f"{progress_value * 100:.0f}%"
            except Exception:
                progress_text = str(payload["progress"])

        print(
            "Poll status:",
            response.status_code,
            "| Progress:",
            progress_text,
            "| Retry-After:",
            retry_after,
        )

        now = monotonic()
        if progress_text != last_progress_text:
            last_progress_text = progress_text
            last_progress_change_at = now
        elapsed = now - started_at
        stalled = now - last_progress_change_at
        if elapsed >= SIMULATION_WAIT_MAX_SECONDS:
            raise RuntimeError(
                "Simulation polling timed out: "
                f"elapsed={elapsed:.0f}s max={SIMULATION_WAIT_MAX_SECONDS}s "
                f"progress={progress_text} progress_url={progress_url}"
            )
        if stalled >= SIMULATION_STALLED_MAX_SECONDS:
            raise RuntimeError(
                "Simulation polling stalled: "
                f"stalled={stalled:.0f}s max={SIMULATION_STALLED_MAX_SECONDS}s "
                f"progress={progress_text} progress_url={progress_url}"
            )

        if retry_after == 0:
            if isinstance(payload, dict):
                return payload
            return response.json()

        sleep(retry_after)


def fetch_alpha_details(session: requests.Session, alpha_id: str) -> dict:
    max_attempts = int(os.getenv("BRAIN_ALPHA_DETAILS_MAX_RETRIES", "6"))
    base_delay = float(os.getenv("BRAIN_ALPHA_DETAILS_RETRY_SECONDS", "8"))
    last_text = ""
    for attempt in range(1, max(1, max_attempts) + 1):
        response = session.get(f"{BASE_URL}/alphas/{alpha_id}")
        if response.status_code == 200:
            return response.json()
        last_text = response.text
        rate_limited = response.status_code == 429 or "rate limit" in response.text.lower()
        if not rate_limited or attempt >= max_attempts:
            break
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else base_delay * attempt
        except Exception:
            delay = base_delay * attempt
        sleep(min(120.0, max(1.0, delay)))
    raise RuntimeError(f"Failed to fetch alpha details: {last_text}")


def fetch_submit_checks_preview(session: requests.Session, alpha_id: str) -> dict | None:
    response = session.get(f"{BASE_URL}/alphas/{alpha_id}/check")

    if response.status_code not in (200, 201, 403):
        return None

    try:
        payload = response.json()
    except Exception:
        return None

    if isinstance(payload, dict) and ((payload.get("is") or {}).get("checks")):
        return payload
    return None


def count_pending_checks(submit_preview: dict | None) -> int:
    if not submit_preview:
        return 0
    checks = ((submit_preview.get("is") or {}).get("checks")) or []
    return sum(1 for check in checks if check.get("result") == "PENDING")


def submit_preview_has_final_failure(submit_preview: dict | None) -> bool:
    if not submit_preview:
        return False
    checks = ((submit_preview.get("is") or {}).get("checks")) or []
    return any(
        check.get("result") == "FAIL"
        and check.get("name") != "ALREADY_SUBMITTED"
        for check in checks
    )


def fetch_submit_checks_preview_with_retry(
    session: requests.Session,
    alpha_id: str,
    max_retries: int = CHECK_PREVIEW_MAX_RETRIES,
    retry_delay_seconds: int = CHECK_PREVIEW_RETRY_DELAY_SECONDS,
) -> dict | None:
    submit_preview = fetch_submit_checks_preview(session, alpha_id)
    pending_count = count_pending_checks(submit_preview)

    if submit_preview and pending_count == 0:
        return submit_preview

    if submit_preview_has_final_failure(submit_preview):
        print("Check preview has failed check(s); skip pending retry.")
        return submit_preview

    if max_retries <= 0:
        return submit_preview

    if submit_preview:
        print(
            f"Check preview still has {pending_count} pending check(s). "
            f"Will retry up to {max_retries} time(s)."
        )
    else:
        print(
            "Check preview not available yet. "
            f"Will retry up to {max_retries} time(s)."
        )

    for attempt in range(1, max_retries + 1):
        print(
            f"Check preview retry {attempt}/{max_retries} after "
            f"{retry_delay_seconds}s..."
        )
        sleep(retry_delay_seconds)
        submit_preview = fetch_submit_checks_preview(session, alpha_id)
        pending_count = count_pending_checks(submit_preview)
        if submit_preview and pending_count == 0:
            print("Check preview resolved after retry.")
            return submit_preview
        if submit_preview_has_final_failure(submit_preview):
            print("Check preview has failed check(s); stop pending retry.")
            return submit_preview

    if submit_preview:
        print(f"Check preview still has {pending_count} pending check(s) after retries.")
    else:
        print("Check preview still unavailable after retries.")
    return submit_preview


def build_properties_patch(config: dict) -> tuple[dict, list[str]]:
    patch = {}
    warnings = []

    if config.get("name"):
        patch["name"] = str(config["name"])

    if "tags" in config:
        tags = config.get("tags") or []
        if isinstance(tags, list):
            patch["tags"] = [str(tag) for tag in tags]
        else:
            warnings.append("tags must be a list; skipping tags sync.")

    color = normalize_color(config.get("color"))
    if config.get("color") and not color:
        warnings.append(
            f"Unsupported color '{config.get('color')}'. "
            "Use one of: red, yellow, green, blue, purple."
        )
    elif color:
        patch["color"] = color

    category = normalize_category(config.get("category"))
    if config.get("category") and not category:
        warnings.append(
            f"Unsupported category '{config.get('category')}'. "
            "Use one of: PRICE_REVERSION, PRICE_MOMENTUM, VOLUME, "
            "FUNDAMENTAL, ANALYST, PRICE_VOLUME, RELATION, SENTIMENT."
        )
    elif category:
        patch["category"] = category

    description = config.get("description")
    if description is not None:
        patch["regular"] = {"description": str(description).strip()}

    return patch, warnings


def sync_alpha_properties(session: requests.Session, alpha_id: str, config: dict) -> dict | None:
    patch, warnings = build_properties_patch(config)

    for warning in warnings:
        print("Properties warning:", warning)

    if not patch:
        print("Properties sync skipped: no supported property fields found.")
        return None

    response = session.patch(f"{BASE_URL}/alphas/{alpha_id}", json=patch)
    print("Properties sync status:", response.status_code)

    if response.status_code != 200:
        raise RuntimeError(f"Failed to sync alpha properties: {response.text}")

    return response.json()


def extract_effective_checks(alpha_details: dict) -> list[dict]:
    submit_preview_checks = ((alpha_details.get("submitPreview") or {}).get("is") or {}).get("checks")
    if submit_preview_checks:
        return submit_preview_checks

    submit_checks = ((alpha_details.get("is") or {}).get("submitChecks")) or []
    if submit_checks:
        return submit_checks

    return ((alpha_details.get("is") or {}).get("checks")) or []


def get_check_buckets(alpha_details: dict) -> tuple[list[dict], list[dict], list[dict]]:
    checks = extract_effective_checks(alpha_details)
    failed = [
        check
        for check in checks
        if check.get("result") == "FAIL"
        and check.get("name") != "ALREADY_SUBMITTED"
    ]
    pending = [check for check in checks if check.get("result") == "PENDING"]
    passed = [
        check
        for check in checks
        if check.get("result") == "PASS"
        or (check.get("result") == "FAIL" and check.get("name") == "ALREADY_SUBMITTED")
    ]
    return passed, failed, pending


def check_is_passed(check: dict) -> bool:
    return check.get("result") == "PASS" or (
        check.get("result") == "FAIL" and check.get("name") == "ALREADY_SUBMITTED"
    )


def submit_check_pass_count(alpha_details: dict) -> int:
    return sum(1 for check in extract_effective_checks(alpha_details) if check_is_passed(check))


def correlation_check_passed(alpha_details: dict) -> bool:
    checks = extract_effective_checks(alpha_details)
    correlation_checks = [
        check
        for check in checks
        if str(check.get("name") or "").upper() in CORRELATION_CHECK_NAMES
    ]
    if not correlation_checks:
        return submit_check_pass_count(alpha_details) >= SUBMIT_PASS_GATE_COUNT
    return all(check_is_passed(check) for check in correlation_checks)


def correlation_check_pending(alpha_details: dict) -> bool:
    checks = extract_effective_checks(alpha_details)
    return any(
        str(check.get("name") or "").upper() in CORRELATION_CHECK_NAMES
        and check.get("result") == "PENDING"
        for check in checks
    )


def normalize_grade_label(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).strip().upper()


def format_color_label(color: str | None) -> str:
    return color if color else "WHITE"


def derive_auto_color(alpha_details: dict) -> str | None:
    _, failed, _ = get_check_buckets(alpha_details)
    if failed:
        return "RED"

    pass_count = submit_check_pass_count(alpha_details)
    if pass_count < BASE_PASS_GATE_COUNT:
        return "RED"
    if correlation_check_pending(alpha_details):
        return None
    if not correlation_check_passed(alpha_details):
        return "RED"
    if pass_count < SUBMIT_PASS_GATE_COUNT:
        return "RED"

    grade = normalize_grade_label(alpha_details.get("grade"))
    mapped = GRADE_TO_COLOR.get(grade)
    if mapped:
        return mapped
    return None


def derive_auto_grade_tag(alpha_details: dict) -> str | None:
    color = derive_auto_color(alpha_details)
    if not color:
        return None
    inferred_grade = COLOR_TO_GRADE.get(color)
    if not inferred_grade:
        return None
    return f"{GRADE_TAG_PREFIX}{inferred_grade}"


def merge_grade_tag(existing_tags: list[str] | None, grade_tag: str | None) -> list[str]:
    tags = [str(tag) for tag in (existing_tags or []) if str(tag).strip()]
    grade_values = set(GRADE_TO_COLOR.keys())
    merged = []
    for tag in tags:
        normalized = normalize_grade_label(tag)
        if normalized == REPAIR_TAG or normalized.startswith(REPAIR_FAMILY_TAG_PREFIX):
            merged.append(tag)
            continue
        bare = normalized[1:] if normalized.startswith(GRADE_TAG_PREFIX) else normalized
        if bare in grade_values:
            continue
        merged.append(tag)
    if grade_tag:
        merged.append(grade_tag)
    return merged


def is_platform_family_tag(tag: str) -> bool:
    return str(tag or "").strip().upper().startswith(REPAIR_FAMILY_TAG_PREFIX)


def slugify_family_tag(value: str, limit: int = 96) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_").upper()
    slug = re.sub(r"_+", "_", slug)
    return slug[:limit] or "UNKNOWN"


def infer_platform_family_tag(config: dict | None = None, source_file: str | None = None, batch_name: str | None = None) -> str | None:
    config = config or {}
    existing_tags = [str(tag) for tag in (config.get("tags") or []) if str(tag).strip()]
    for tag in existing_tags:
        normalized = normalize_grade_label(tag)
        if is_platform_family_tag(normalized):
            return normalized

    explicit_family = str(config.get("raw_alpha_family") or config.get("family") or "").strip()
    if explicit_family:
        return f"{REPAIR_FAMILY_TAG_PREFIX}{slugify_family_tag(explicit_family)}"

    source_text = " ".join(str(item or "") for item in [source_file, batch_name, config.get("name")])
    match = re.search(r"(?:raw_seed_|raw_alpha_)(.+?)(?:_lane\d+|\.yaml|:|>|$)", source_text)
    if match:
        return f"{REPAIR_FAMILY_TAG_PREFIX}{slugify_family_tag(match.group(1))}"
    for token in source_text.replace(">", " ").replace("<", " ").split():
        if token.startswith("research_paper_") or token.startswith("credit_recovery_"):
            return f"{REPAIR_FAMILY_TAG_PREFIX}{slugify_family_tag(token.split(':', 1)[0])}"
    return None


def sanitize_platform_tags(
    tags: list[str] | None,
    grade_tag: str | None = None,
    keep_repair: bool = True,
    family_tag: str | None = None,
) -> list[str]:
    grade_values = set(GRADE_TO_COLOR.keys())
    output: list[str] = []
    seen: set[str] = set()
    normalized_family_tag = normalize_grade_label(family_tag) if family_tag else None

    def add(tag: str) -> None:
        normalized = normalize_grade_label(tag)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        output.append(str(tag))

    for tag in tags or []:
        normalized = normalize_grade_label(str(tag))
        if not normalized:
            continue
        bare = normalized[1:] if normalized.startswith(GRADE_TAG_PREFIX) else normalized
        if bare in grade_values:
            continue
        if normalized == REPAIR_TAG:
            if keep_repair:
                add(REPAIR_TAG)
            continue
        if is_platform_family_tag(normalized):
            if not normalized_family_tag:
                add(normalized)
            elif normalized == normalized_family_tag:
                add(normalized_family_tag)
    if grade_tag:
        add(grade_tag)
    if normalized_family_tag:
        add(normalized_family_tag)
    return output


def has_expected_grade_tag(alpha_details: dict) -> bool:
    grade_tag = derive_auto_grade_tag(alpha_details)
    if not grade_tag:
        return False
    tags = [normalize_grade_label(str(tag)) for tag in (alpha_details.get("tags") or [])]
    return grade_tag in tags


def sync_auto_color(
    session: requests.Session,
    alpha_id: str,
    alpha_details: dict,
    family_tag: str | None = None,
) -> dict | None:
    auto_color = derive_auto_color(alpha_details)
    platform_color = normalize_color(alpha_details.get("color"))
    grade_tag = derive_auto_grade_tag(alpha_details)
    existing_tags = alpha_details.get("tags") or []
    # Repair waiting is governed only by sync_repair_wait_tags.py. Normal color
    # sync must not preserve 1REPAIR on repair descendants or low-grade results;
    # otherwise the platform-visible repair queue grows with non-source alphas.
    keep_repair = False
    merged_tags = sanitize_platform_tags(
        existing_tags,
        grade_tag=grade_tag,
        keep_repair=keep_repair,
        family_tag=family_tag,
    )
    tags_changed = merged_tags != [str(tag) for tag in existing_tags]
    if auto_color is None:
        print("Auto color sync skipped:", alpha_id, "| Color: WHITE | reason=undetermined")
        return None
    if platform_color == auto_color and not tags_changed:
        print(
            "Auto color sync skipped:",
            alpha_id,
            "| Color:",
            format_color_label(auto_color),
            "| GradeTag:",
            grade_tag or "<none>",
            "| reason=already_synced",
        )
        return None
    patch = {"color": auto_color, "tags": merged_tags}
    response = session.patch(f"{BASE_URL}/alphas/{alpha_id}", json=patch)
    print(
        "Auto color sync status:",
        response.status_code,
        "| Color:",
        format_color_label(auto_color),
        "| GradeTag:",
        grade_tag or "<none>",
    )
    if response.status_code != 200:
        raise RuntimeError(f"Failed to sync auto color: {response.text}")
    return response.json()


def merge_submit_preview(alpha_details: dict, submit_preview: dict | None) -> dict:
    if not submit_preview:
        return alpha_details

    merged = dict(alpha_details)
    merged_is = dict(merged.get("is") or {})
    preview_is = submit_preview.get("is") or {}

    if preview_is.get("checks"):
        merged_is["submitChecks"] = preview_is["checks"]
    if preview_is.get("selfCorrelated"):
        merged_is["selfCorrelated"] = preview_is["selfCorrelated"]

    merged["is"] = merged_is
    merged["submitPreview"] = submit_preview
    return merged


def alpha_display_name(config: dict | None, alpha_details: dict | None) -> str | None:
    if alpha_details and alpha_details.get("name"):
        return alpha_details["name"]
    if config and config.get("name"):
        return str(config["name"])
    if alpha_details and alpha_details.get("id"):
        return alpha_details["id"]
    return None


def alpha_display_category(config: dict | None, alpha_details: dict | None) -> str | None:
    if alpha_details and alpha_details.get("category"):
        return alpha_details["category"]
    if config and config.get("category"):
        normalized = normalize_category(config.get("category"))
        return normalized or str(config["category"])
    return None


def alpha_display_color(config: dict | None, alpha_details: dict | None) -> str | None:
    if alpha_details and alpha_details.get("color"):
        return alpha_details["color"]
    if config and config.get("color"):
        normalized = normalize_color(config.get("color"))
        return normalized or str(config["color"])
    return None


def alpha_display_tags(config: dict | None, alpha_details: dict | None) -> list[str]:
    if alpha_details and alpha_details.get("tags"):
        return alpha_details["tags"]
    if config and config.get("tags"):
        return [str(tag) for tag in (config.get("tags") or [])]
    return []


def alpha_display_description(config: dict | None, alpha_details: dict | None) -> str | None:
    regular = (alpha_details or {}).get("regular") or {}
    if regular.get("description"):
        return regular["description"]
    if config and config.get("description") is not None:
        return str(config.get("description")).strip()
    return None


def print_metric_block(title: str, data: dict | None) -> None:
    if not data:
        print(f"{title}: none")
        return

    print(
        f"{title}: "
        f"Sharpe={data.get('sharpe')} | "
        f"Fitness={data.get('fitness')} | "
        f"Returns={data.get('returns')} | "
        f"Turnover={data.get('turnover')} | "
        f"Drawdown={data.get('drawdown')} | "
        f"Margin={data.get('margin')}"
    )


def print_platform_checks(alpha_details: dict) -> None:
    checks = ((alpha_details.get("is") or {}).get("checks")) or []
    if not checks:
        print("Platform checks: none returned")
        return

    print("Platform checks:")
    for check in checks:
        parts = [check.get("name", "UNKNOWN"), check.get("result", "UNKNOWN")]
        if "limit" in check:
            parts.append(f"limit={check['limit']}")
        if "value" in check:
            parts.append(f"value={check['value']}")
        print(" - " + " | ".join(parts))


def print_check_summary(alpha_details: dict) -> None:
    checks = extract_effective_checks(alpha_details)
    if not checks:
        return

    failed = [check for check in checks if check.get("result") == "FAIL"]
    pending = [check for check in checks if check.get("result") == "PENDING"]
    passed = [check for check in checks if check.get("result") == "PASS"]

    print("Check summary:")
    print(f" - Passed: {len(passed)}")
    print(f" - Failed: {len(failed)}")
    print(f" - Pending: {len(pending)}")

    if failed:
        print("Failed checks detail:")
        for check in failed:
            parts = [check.get("name", "UNKNOWN")]
            if "limit" in check:
                parts.append(f"limit={check['limit']}")
            if "value" in check:
                parts.append(f"value={check['value']}")
            print(" - " + " | ".join(parts))

    if pending:
        print("Pending checks detail:")
        for check in pending:
            print(" - " + check.get("name", "UNKNOWN"))


def print_submit_preview(submit_preview: dict | None) -> None:
    if not submit_preview:
        return

    checks = ((submit_preview.get("is") or {}).get("checks")) or []
    if not checks:
        return

    print("Check API results:")
    for check in checks:
        parts = [check.get("name", "UNKNOWN"), check.get("result", "UNKNOWN")]
        if "limit" in check:
            parts.append(f"limit={check['limit']}")
        if "value" in check:
            parts.append(f"value={check['value']}")
        print(" - " + " | ".join(parts))

    pending_count = count_pending_checks(submit_preview)
    if pending_count:
        print(f"Check API unresolved pending count: {pending_count}")


def print_scorecard(alpha_details: dict, config: dict | None = None, submit_preview: dict | None = None) -> None:
    auto_color = derive_auto_color(alpha_details)
    print("=" * 60)
    print("Alpha scorecard")
    print("Alpha ID:", alpha_details.get("id"))
    print("Status:", alpha_details.get("status"))
    print("Stage:", alpha_details.get("stage"))
    print("Grade:", alpha_details.get("grade"))
    print("Name:", alpha_display_name(config, alpha_details))
    print("Category:", alpha_display_category(config, alpha_details))
    print("Color:", alpha_display_color(config, alpha_details))
    print("Rule Color:", format_color_label(auto_color))
    print("Tags:", alpha_display_tags(config, alpha_details))
    print_metric_block("IS", alpha_details.get("is"))
    print_metric_block("TRAIN", alpha_details.get("train"))
    print_metric_block("TEST", alpha_details.get("test"))
    print_metric_block("OS", alpha_details.get("os"))
    print_platform_checks(alpha_details)
    print_check_summary(alpha_details)
    print_submit_preview(submit_preview)


def build_result_payload(
    alpha_id: str,
    config: dict,
    alpha_details: dict,
    source_file: str | None = None,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> dict:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "alpha_id": alpha_id,
        "name": config.get("name"),
        "category": config.get("category"),
        "tags": config.get("tags"),
        "color": config.get("color"),
        "description": config.get("description"),
        "expression": config.get("expression"),
        "settings": config.get("settings"),
        "source_file": source_file,
        "batch_name": batch_name,
        "storage_mode": storage_mode,
        "fingerprint": build_alpha_fingerprint(config),
        "rule_color": derive_auto_color(alpha_details),
        "display": {
            "name": alpha_display_name(config, alpha_details),
            "category": alpha_display_category(config, alpha_details),
            "color": alpha_display_color(config, alpha_details),
            "tags": alpha_display_tags(config, alpha_details),
            "description": alpha_display_description(config, alpha_details),
        },
        "alpha_details": alpha_details,
    }


def alpha_truth_record(
    alpha_id: str,
    config: dict,
    alpha_details: dict,
    source_file: str | None = None,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> dict:
    tags = [str(tag) for tag in alpha_display_tags(config, alpha_details) if str(tag).strip()]
    grade_tag = derive_auto_grade_tag(alpha_details)
    checks = extract_effective_checks(alpha_details)
    passed, failed, pending = get_check_buckets(alpha_details)
    platform_grade = normalize_grade_label(alpha_details.get("grade"))
    family = ""
    source_text = " ".join(str(item or "") for item in [source_file, batch_name, config.get("name")])
    match = re.search(r"(?:raw_seed_|raw_alpha_)(.+?)(?:_lane\d+|\.yaml|$)", source_text)
    if match:
        family = match.group(1)
    return {
        "alpha_id": alpha_id,
        "updated_at": utc_now(),
        "status": alpha_details.get("status"),
        "stage": alpha_details.get("stage"),
        "grade": platform_grade,
        "color": alpha_details.get("color"),
        "rule_color": derive_auto_color(alpha_details),
        "rule_grade_tag": grade_tag,
        "platform_grade_tag": next(
            (
                normalize_grade_label(tag)
                for tag in tags
                if normalize_grade_label(tag) in {f"1{grade}" for grade in GRADE_TO_COLOR}
            ),
            None,
        ),
        "tags": tags,
        "has_grade_tag": any(normalize_grade_label(tag) in {f"1{grade}" for grade in GRADE_TO_COLOR} for tag in tags),
        "pass_count": len(passed),
        "failed_checks": [check.get("name") for check in failed],
        "pending_checks": [check.get("name") for check in pending],
        "checks": [{"name": check.get("name"), "result": check.get("result")} for check in checks],
        "is_submit_ready": bool(grade_tag and len(passed) >= SUBMIT_PASS_GATE_COUNT and not failed and not pending),
        "family": family,
        "source_file": source_file,
        "batch_name": batch_name,
        "storage_mode": storage_mode,
        "name": alpha_display_name(config, alpha_details),
        "expression": config.get("expression"),
        "settings": config.get("settings"),
    }


def update_alpha_truth_state(
    alpha_id: str,
    config: dict,
    alpha_details: dict,
    source_file: str | None = None,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> None:
    payload = read_json_file(ALPHA_TRUTH_STATE_FILE, {})
    if not isinstance(payload, dict):
        payload = {}
    records = payload.get("alphas")
    if not isinstance(records, dict):
        records = {}
    record = alpha_truth_record(
        alpha_id,
        config,
        alpha_details,
        source_file=source_file,
        batch_name=batch_name,
        storage_mode=storage_mode,
    )
    records[alpha_id] = record
    payload.update(
        {
            "schema_version": 1,
            "updated_at": utc_now(),
            "source": "persist_alpha_state_after_refresh_and_sync",
            "alphas": records,
        }
    )
    ALPHA_TRUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALPHA_TRUTH_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def should_keep_full_result_snapshot(alpha_details: dict) -> bool:
    auto_color = derive_auto_color(alpha_details)
    return auto_color in IMPORTANT_RESULT_COLORS or auto_color is None


def build_catalog_payload(result_payload: dict) -> dict:
    alpha_details = result_payload.get("alpha_details") or {}
    compact_details = {
        "id": alpha_details.get("id"),
        "status": alpha_details.get("status"),
        "stage": alpha_details.get("stage"),
        "grade": alpha_details.get("grade"),
        "name": alpha_details.get("name"),
        "color": alpha_details.get("color"),
        "category": alpha_details.get("category"),
        "is": alpha_details.get("is"),
        "train": alpha_details.get("train"),
        "test": alpha_details.get("test"),
        "os": alpha_details.get("os"),
        "submitPreview": alpha_details.get("submitPreview"),
    }
    return {
        "schema_version": result_payload.get("schema_version"),
        "alpha_id": result_payload.get("alpha_id"),
        "name": result_payload.get("name"),
        "category": result_payload.get("category"),
        "tags": result_payload.get("tags"),
        "color": result_payload.get("color"),
        "description": result_payload.get("description"),
        "expression": result_payload.get("expression"),
        "settings": result_payload.get("settings"),
        "source_file": result_payload.get("source_file"),
        "batch_name": result_payload.get("batch_name"),
        "storage_mode": result_payload.get("storage_mode"),
        "fingerprint": result_payload.get("fingerprint"),
        "rule_color": result_payload.get("rule_color"),
        "display": result_payload.get("display"),
        "alpha_details": compact_details,
    }


def extract_source_context(source_file: str | None) -> dict:
    if not source_file:
        return {
            "source_kind": None,
            "template_name": None,
            "field": None,
        }

    source_text = str(source_file)
    if source_text.startswith("<memory:") and source_text.endswith(">"):
        body = source_text[len("<memory:") : -1]
        template_name, _, field = body.rpartition(":")
        return {
            "source_kind": "memory_template_loop",
            "template_name": template_name or None,
            "field": field or None,
        }

    source_path = Path(source_text)
    if source_path.suffix.lower() in {".yaml", ".yml"}:
        return {
            "source_kind": "yaml_file",
            "template_name": source_path.name,
            "field": None,
        }

    return {
        "source_kind": "unknown",
        "template_name": None,
        "field": None,
    }


def build_template_field_payload(result_payload: dict) -> dict:
    alpha_details = result_payload.get("alpha_details") or {}
    is_block = alpha_details.get("is") or {}
    source_context = extract_source_context(result_payload.get("source_file"))
    return {
        "schema_version": result_payload.get("schema_version"),
        "alpha_id": result_payload.get("alpha_id"),
        "batch_name": result_payload.get("batch_name"),
        "template_name": source_context.get("template_name"),
        "field": source_context.get("field"),
        "source_kind": source_context.get("source_kind"),
        "source_file": result_payload.get("source_file"),
        "name": result_payload.get("name"),
        "category": result_payload.get("category"),
        "rule_color": result_payload.get("rule_color"),
        "grade": alpha_details.get("grade"),
        "status": alpha_details.get("status"),
        "stage": alpha_details.get("stage"),
        "fingerprint": result_payload.get("fingerprint"),
        "sharpe": is_block.get("sharpe"),
        "fitness": is_block.get("fitness"),
        "returns": is_block.get("returns"),
        "turnover": is_block.get("turnover"),
        "drawdown": is_block.get("drawdown"),
        "margin": is_block.get("margin"),
        "display": result_payload.get("display"),
    }


def save_alpha_result(
    alpha_id: str,
    config: dict,
    alpha_details: dict,
    source_file: str | None = None,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> Path:
    ensure_local_state_directories()
    payload = build_result_payload(
        alpha_id,
        config,
        alpha_details,
        source_file=source_file,
        batch_name=batch_name,
        storage_mode=storage_mode,
    )

    append_jsonl(
        RESULTS_INDEX_FINGERPRINTS_FILE,
        {
            "alpha_id": alpha_id,
            "fingerprint": payload["fingerprint"],
            "rule_color": payload.get("rule_color"),
            "batch_name": batch_name,
            "storage_mode": storage_mode,
        },
    )
    append_jsonl(RESULTS_INDEX_CATALOG_FILE, build_catalog_payload(payload))
    append_jsonl(
        RESULTS_INDEX_TEMPLATE_FIELD_FILE,
        build_template_field_payload(payload),
    )

    if batch_name:
        batch_path = RESULTS_BATCHES_DIR / f"{batch_name}.jsonl"
        append_jsonl(batch_path, payload)
    else:
        batch_path = RESULTS_BATCHES_DIR / "adhoc.jsonl"
        append_jsonl(batch_path, payload)

    if storage_mode == "full" or should_keep_full_result_snapshot(alpha_details):
        result_path = RESULTS_RAW_DIR / f"{alpha_id}.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return result_path

    return batch_path


def build_experience_summary(config: dict, alpha_details: dict) -> str:
    passed, failed, pending = get_check_buckets(alpha_details)
    effective_checks = extract_effective_checks(alpha_details)
    auto_color = derive_auto_color(alpha_details) or "WHITE"

    lines = [
        f"# {alpha_details.get('id')} - {alpha_display_name(config, alpha_details) or 'anonymous'}",
        "",
        "## Summary",
        f"- Grade: {alpha_details.get('grade')}",
        f"- Auto Color Rule: {auto_color}",
        f"- Status: {alpha_details.get('status')}",
        f"- Stage: {alpha_details.get('stage')}",
        f"- Name: {alpha_display_name(config, alpha_details)}",
        f"- Category: {alpha_display_category(config, alpha_details)}",
        f"- Color: {alpha_display_color(config, alpha_details)}",
        f"- Tags: {alpha_display_tags(config, alpha_details)}",
        f"- Expression: `{str(config.get('expression', '')).strip()}`",
        "",
        "## IS Metrics",
        f"- Sharpe: {((alpha_details.get('is') or {}).get('sharpe'))}",
        f"- Fitness: {((alpha_details.get('is') or {}).get('fitness'))}",
        f"- Returns: {((alpha_details.get('is') or {}).get('returns'))}",
        f"- Turnover: {((alpha_details.get('is') or {}).get('turnover'))}",
        f"- Drawdown: {((alpha_details.get('is') or {}).get('drawdown'))}",
        f"- Margin: {((alpha_details.get('is') or {}).get('margin'))}",
        "",
        "## Checks",
        f"- Passed: {len(passed)}",
        f"- Failed: {len(failed)}",
        f"- Pending: {len(pending)}",
    ]

    if failed:
        lines.extend(["", "## Failed Checks"])
        for check in failed:
            lines.append(
                f"- {check.get('name')}: limit={check.get('limit')} | value={check.get('value')}"
            )

    if pending:
        lines.extend(["", "## Pending Checks"])
        for check in pending:
            lines.append(f"- {check.get('name')}")

    if effective_checks:
        lines.extend(["", "## Effective Checks Source"])
        if ((alpha_details.get("submitPreview") or {}).get("is") or {}).get("checks"):
            lines.append("- check api preview")
        elif ((alpha_details.get("is") or {}).get("submitChecks")):
            lines.append("- merged check api preview")
        else:
            lines.append("- regular alpha details")

    lines.extend(
        [
            "",
            "## Settings",
            "```json",
            json.dumps(config.get("settings", {}), ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def get_experience_target_dir(alpha_details: dict) -> Path:
    _, failed, pending = get_check_buckets(alpha_details)
    auto_color = derive_auto_color(alpha_details)

    if failed:
        return EXPERIENCE_FAILURES_DIR
    if pending:
        return EXPERIENCE_PENDING_DIR
    if auto_color == "YELLOW":
        return EXPERIENCE_AVERAGE_DIR
    if auto_color == "GREEN":
        return EXPERIENCE_GOOD_DIR
    if auto_color == "BLUE":
        return EXPERIENCE_EXCELLENT_DIR
    if auto_color == "PURPLE":
        return EXPERIENCE_SPECTACULAR_DIR
    return EXPERIENCE_PATTERNS_DIR


def remove_old_experience_notes(alpha_id: str) -> None:
    for directory in EXPERIENCE_NOTE_DIRS:
        note_path = directory / f"{alpha_id}.md"
        if note_path.exists():
            note_path.unlink()


def experience_log_name(alpha_details: dict) -> str:
    auto_color = derive_auto_color(alpha_details)
    if auto_color == "RED":
        return "red.jsonl"
    if auto_color is None:
        return "white.jsonl"
    return f"{auto_color.lower()}.jsonl"


def save_experience_note(alpha_id: str, config: dict, alpha_details: dict) -> Path:
    ensure_local_state_directories()
    append_jsonl(
        EXPERIENCE_LOGS_DIR / experience_log_name(alpha_details),
        {
            "alpha_id": alpha_id,
            "name": alpha_display_name(config, alpha_details),
            "category": alpha_display_category(config, alpha_details),
            "rule_color": derive_auto_color(alpha_details) or "WHITE",
            "grade": alpha_details.get("grade"),
            "status": alpha_details.get("status"),
            "stage": alpha_details.get("stage"),
            "expression": config.get("expression"),
            "settings": config.get("settings"),
            "source_file": config.get("source_file"),
        },
    )
    remove_old_experience_notes(alpha_id)
    auto_color = derive_auto_color(alpha_details)
    if auto_color not in IMPORTANT_EXPERIENCE_COLORS:
        return EXPERIENCE_LOGS_DIR / experience_log_name(alpha_details)
    target_dir = get_experience_target_dir(alpha_details)
    note_path = target_dir / f"{alpha_id}.md"
    note_path.write_text(build_experience_summary(config, alpha_details), encoding="utf-8")
    return note_path


def refresh_alpha_state(
    session: requests.Session,
    alpha_id: str,
    check_retries: int = CHECK_PREVIEW_MAX_RETRIES,
    check_retry_delay_seconds: int = CHECK_PREVIEW_RETRY_DELAY_SECONDS,
) -> tuple[dict, dict | None]:
    alpha_details = fetch_alpha_details(session, alpha_id)
    submit_preview = fetch_submit_checks_preview_with_retry(
        session,
        alpha_id,
        max_retries=check_retries,
        retry_delay_seconds=check_retry_delay_seconds,
    )
    merged_alpha_details = merge_submit_preview(alpha_details, submit_preview)
    return merged_alpha_details, submit_preview


def persist_alpha_state(
    alpha_id: str,
    config: dict,
    alpha_details: dict,
    source_file: str | None = None,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> tuple[Path, Path]:
    result_path = save_alpha_result(
        alpha_id,
        config,
        alpha_details,
        source_file=source_file,
        batch_name=batch_name,
        storage_mode=storage_mode,
    )
    experience_path = save_experience_note(alpha_id, config, alpha_details)
    update_alpha_truth_state(
        alpha_id,
        config,
        alpha_details,
        source_file=source_file,
        batch_name=batch_name,
        storage_mode=storage_mode,
    )
    return result_path, experience_path


def iter_all_result_payloads() -> list[dict]:
    latest_by_alpha: dict[str, dict] = {}

    for result_path in sorted(RESULTS_DIR.glob("*.json")):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        alpha_id = payload.get("alpha_id")
        if alpha_id:
            latest_by_alpha[alpha_id] = payload

    if RESULTS_RAW_DIR.exists():
        for result_path in sorted(RESULTS_RAW_DIR.glob("*.json")):
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            alpha_id = payload.get("alpha_id")
            if alpha_id:
                latest_by_alpha[alpha_id] = payload

    if RESULTS_INDEX_CATALOG_FILE.exists():
        with RESULTS_INDEX_CATALOG_FILE.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                alpha_id = payload.get("alpha_id")
                if alpha_id:
                    latest_by_alpha[alpha_id] = payload

    return list(latest_by_alpha.values())


def submit_alpha_file(
    session: requests.Session,
    alpha_file: Path,
    check_retries: int = CHECK_PREVIEW_MAX_RETRIES,
    check_retry_delay_seconds: int = CHECK_PREVIEW_RETRY_DELAY_SECONDS,
    sync_platform_properties: bool = True,
    sync_platform_color: bool = True,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> dict:
    config = load_alpha_config(alpha_file)
    return submit_alpha_config(
        session=session,
        config=config,
        source_label=str(alpha_file),
        source_file=str(alpha_file),
        fallback_name=alpha_file.stem,
        check_retries=check_retries,
        check_retry_delay_seconds=check_retry_delay_seconds,
        sync_platform_properties=sync_platform_properties,
        sync_platform_color=sync_platform_color,
        batch_name=batch_name,
        storage_mode=storage_mode,
    )


def submit_alpha_config(
    session: requests.Session,
    config: dict,
    source_label: str = "<memory>",
    source_file: str | None = None,
    fallback_name: str | None = None,
    check_retries: int = CHECK_PREVIEW_MAX_RETRIES,
    check_retry_delay_seconds: int = CHECK_PREVIEW_RETRY_DELAY_SECONDS,
    sync_platform_properties: bool = True,
    sync_platform_color: bool = True,
    batch_name: str | None = None,
    storage_mode: str = "full",
) -> dict:
    config = validate_alpha_config(config, source_name=source_label)
    simulation_data = build_simulation_data_from_config(config)
    alpha_name = config.get("name", fallback_name or "anonymous")

    print("=" * 60)
    print("Alpha source:", source_label)
    print("Alpha name:", alpha_name)
    print("Expression:")
    print(config["expression"])
    print("Settings:", simulation_data["settings"])

    progress_url = submit_simulation(session, simulation_data)
    result = wait_for_simulation(session, progress_url)
    alpha_id = result.get("alpha")
    print("Alpha ID:", alpha_id)

    if not alpha_id:
        raise RuntimeError(f"Simulation finished without alpha id: {result}")

    synced_properties = None
    if sync_platform_properties:
        synced_properties = sync_alpha_properties(session, alpha_id, config)

    merged_alpha_details, submit_preview = refresh_alpha_state(
        session,
        alpha_id,
        check_retries=check_retries,
        check_retry_delay_seconds=check_retry_delay_seconds,
    )

    family_tag = infer_platform_family_tag(config, source_file=source_file or source_label, batch_name=batch_name)

    auto_color_response = None
    if sync_platform_color:
        auto_color_response = sync_auto_color(session, alpha_id, merged_alpha_details, family_tag=family_tag)
        merged_alpha_details, submit_preview = refresh_alpha_state(
            session,
            alpha_id,
            check_retries=0,
            check_retry_delay_seconds=check_retry_delay_seconds,
        )

    print_scorecard(merged_alpha_details, config=config, submit_preview=submit_preview)
    result_path, experience_path = persist_alpha_state(
        alpha_id,
        config,
        merged_alpha_details,
        source_file=source_file or source_label,
        batch_name=batch_name,
        storage_mode=storage_mode,
    )
    print("Saved result file:", result_path)
    print("Saved experience note:", experience_path)

    return {
        "name": alpha_name,
        "file": source_label,
        "alpha_id": alpha_id,
        "result": result,
        "alpha_details": merged_alpha_details,
        "submit_preview": submit_preview,
        "properties_synced": synced_properties is not None,
        "auto_color_synced": auto_color_response is not None,
        "result_path": str(result_path),
        "experience_path": str(experience_path),
    }


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise ValueError(
            "brain_client.py now requires explicit YAML path arguments.\n"
            "Example:\n"
            "python brain_client.py temp\\your_alpha.yaml"
        )

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)
    alpha_files = [Path(arg) for arg in args]

    summary = []
    for alpha_file in alpha_files:
        summary.append(submit_alpha_file(session, alpha_file))

    print("=" * 60)
    print(f"Completed {len(summary)} alpha submission(s).")
    for item in summary:
        print(f"- {item['name']}: {item['alpha_id']}")


if __name__ == "__main__":
    main()
