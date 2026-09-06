"""
ABS to StoryGraph Sync Service
"""

from flask import Flask, jsonify, request, render_template, session, redirect, url_for, g
from urllib.parse import urlparse, unquote
from functools import wraps
import os, re, json, logging, threading, time, uuid, html as html_lib
from collections import deque
from datetime import datetime, timezone
import requests as req
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = "/app/data"
USERS_FILE = f"{DATA_DIR}/users.json"
CONFIG_DIR = f"{DATA_DIR}/config"
SYNC_STATE_DIR = f"{DATA_DIR}/sync_state"

STORYGRAPH_BASE = "https://app.thestorygraph.com"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 600))
SYNC_THRESHOLD = float(os.environ.get("SYNC_THRESHOLD_MINUTES", 5))

OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

# Only used as a fallback when the reverse proxy's forwarded headers (picked up
# automatically via ProxyFix below) aren't enough to get the right scheme/host —
# e.g. an unusual proxy chain. Set to the externally-visible base URL, no
# trailing slash: PUBLIC_URL=https://abs-sync.example.com
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")

SYNC_SCOPES = ("in_progress", "in_progress_finished", "library")


def _get_secret_key() -> bytes:
    key_file = f"{DATA_DIR}/secret_key"
    try:
        with open(key_file, "rb") as f:
            key = f.read()
            if len(key) >= 32:
                return key
    except FileNotFoundError:
        pass
    key = os.urandom(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(key_file, "wb") as f:
        f.write(key)
    return key

# ── Logging ───────────────────────────────────────────────────────────────────

class LogBuffer(logging.Handler):
    def __init__(self, maxlen=300):
        super().__init__()
        self._records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self._records.append({
                "time": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            })

    def get(self):
        with self._lock:
            return list(self._records)


_log_buffer = LogBuffer()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger().addHandler(_log_buffer)
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # suppress per-request access logs

# ── User store ───────────────────────────────────────────────────────────────

_users_lock = threading.Lock()


def _load_users() -> list[dict]:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_users(users: list[dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def any_users_exist() -> bool:
    with _users_lock:
        return bool(_load_users())


def list_users() -> list[dict]:
    with _users_lock:
        return _load_users()


def get_user(user_id: str) -> dict | None:
    with _users_lock:
        return next((u for u in _load_users() if u["id"] == user_id), None)


def get_user_by_username(username: str) -> dict | None:
    username = (username or "").lower()
    with _users_lock:
        return next((u for u in _load_users() if (u.get("username") or "").lower() == username), None)


def get_user_by_oidc_sub(sub: str) -> dict | None:
    with _users_lock:
        return next((u for u in _load_users() if u.get("oidc_sub") == sub), None)


def create_user(*, username=None, password=None, oidc_sub=None, display_name=None, is_admin=False) -> dict:
    with _users_lock:
        users = _load_users()
        user = {
            "id": uuid.uuid4().hex,
            "username": username,
            "password_hash": generate_password_hash(password) if password else None,
            "oidc_sub": oidc_sub,
            "display_name": display_name or username or "User",
            "is_admin": is_admin,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        users.append(user)
        _save_users(users)
        return user


def update_user(user_id: str, **fields):
    with _users_lock:
        users = _load_users()
        for u in users:
            if u["id"] == user_id:
                u.update(fields)
        _save_users(users)


def delete_user(user_id: str):
    with _users_lock:
        users = [u for u in _load_users() if u["id"] != user_id]
        _save_users(users)

# ── Per-user config ──────────────────────────────────────────────────────────

_config_lock = threading.Lock()
_runtime_config: dict[str, dict] = {}


def _config_file(user_id: str) -> str:
    return f"{CONFIG_DIR}/{user_id}.json"


def _load_user_config(user_id: str) -> dict:
    try:
        with open(_config_file(user_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_user_config(user_id: str, data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(_config_file(user_id), "w") as f:
        json.dump(data, f, indent=2)


def cfg(user_id: str, key: str, default: str = "") -> str:
    with _config_lock:
        if user_id not in _runtime_config:
            _runtime_config[user_id] = _load_user_config(user_id)
        return _runtime_config[user_id].get(key) or default


def set_cfg(user_id: str, updates: dict):
    with _config_lock:
        if user_id not in _runtime_config:
            _runtime_config[user_id] = _load_user_config(user_id)
        _runtime_config[user_id].update({k: v for k, v in updates.items() if v})
        _save_user_config(user_id, _runtime_config[user_id])

# ── Per-user sync state ──────────────────────────────────────────────────────
# Tracks the last {progress %, StoryGraph status} successfully pushed per book, persisted across restarts.

_synced_state: dict[str, dict] = {}
_synced_state_lock = threading.Lock()


def _sync_state_file(user_id: str) -> str:
    return f"{SYNC_STATE_DIR}/{user_id}.json"


def _load_sync_state(user_id: str) -> dict:
    try:
        with open(_sync_state_file(user_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sync_state(user_id: str, data: dict):
    os.makedirs(SYNC_STATE_DIR, exist_ok=True)
    with open(_sync_state_file(user_id), "w") as f:
        json.dump(data, f, indent=2)


def _get_synced(user_id: str) -> dict:
    with _synced_state_lock:
        if user_id not in _synced_state:
            _synced_state[user_id] = _load_sync_state(user_id)
        return _synced_state[user_id]

# ── ABS ───────────────────────────────────────────────────────────────────────

def _abs_headers(user_id: str) -> dict:
    return {"Authorization": f"Bearer {cfg(user_id, 'ABS_TOKEN')}"}


def _abs_get(user_id: str, path: str, params: dict | None = None):
    resp = req.get(f"{cfg(user_id, 'ABS_URL')}{path}", headers=_abs_headers(user_id), params=params, timeout=10)
    resp.raise_for_status()
    return resp


def _item_to_book(item: dict, progress: dict) -> dict | None:
    # StoryGraph is for books — skip ABS podcasts (and any non-book media).
    if item.get("mediaType") and item.get("mediaType") != "book":
        return None
    media = item.get("media", {})
    metadata = media.get("metadata", {})
    title = metadata.get("title", "").strip()
    if not title:
        return None
    narrator_names = _people_names(metadata.get("narrators")) or _people_names(
        metadata.get("narratorName")
    )
    author_names = _people_names(metadata.get("authors")) or _people_names(
        metadata.get("authorName")
    )
    author_names = _strip_narrators_from_authors(author_names, narrator_names)
    author = ", ".join(author_names)
    asin = str(metadata.get("asin") or "").strip()
    isbn = str(metadata.get("isbn") or metadata.get("isbn13") or "").strip()
    series_name = str(metadata.get("seriesName") or "").strip()
    if not series_name:
        series = metadata.get("series") or []
        names = []
        for s in series:
            if isinstance(s, dict) and s.get("name"):
                names.append(str(s["name"]).strip())
            elif isinstance(s, str) and s.strip():
                names.append(s.strip())
        series_name = ", ".join(names)
    subtitle = str(metadata.get("subtitle") or "").strip()
    return {
        "title": title,
        "author": author,
        "asin": asin,
        "isbn": isbn,
        "series_name": series_name,
        "subtitle": subtitle,
        "prefer_audio": bool(narrator_names),
        "narrators": narrator_names,
        "progress_percent": round((progress.get("progress") or 0) * 100, 1),
        "current_minutes": round((progress.get("currentTime") or 0) / 60, 1),
        "duration_minutes": round((media.get("duration") or 0) / 60, 1),
        "is_finished": bool(progress.get("isFinished")),
        "finished_at": progress.get("finishedAt"),
        "started_at": progress.get("startedAt"),
    }


def get_abs_books(user_id: str, scope: str) -> list[dict]:
    if scope == "in_progress":
        resp = _abs_get(user_id, "/api/me/items-in-progress")
        books = []
        for item in resp.json().get("libraryItems", []):
            item_id = item.get("id", "")
            progress = {}
            if item_id:
                pr = req.get(f"{cfg(user_id, 'ABS_URL')}/api/me/progress/{item_id}", headers=_abs_headers(user_id), timeout=10)
                if pr.status_code == 200:
                    progress = pr.json()
            book = _item_to_book(item, progress)
            if book:
                books.append(book)
        return books

    # in_progress_finished / library both need every progress entry the user has
    me = _abs_get(user_id, "/api/me").json()
    progress_by_item = {p["libraryItemId"]: p for p in me.get("mediaProgress", []) if p.get("libraryItemId")}

    if scope == "in_progress_finished":
        books = []
        for item_id, progress in progress_by_item.items():
            if not ((progress.get("currentTime") or 0) > 0 or progress.get("isFinished")):
                continue
            item_resp = req.get(f"{cfg(user_id, 'ABS_URL')}/api/items/{item_id}", headers=_abs_headers(user_id), timeout=10)
            if item_resp.status_code != 200:
                continue
            book = _item_to_book(item_resp.json(), progress)
            if book:
                books.append(book)
        return books

    # scope == "library": every book, including untouched ones
    libraries = _abs_get(user_id, "/api/libraries").json().get("libraries", [])
    books = []
    for lib in libraries:
        page = 0
        while True:
            resp = _abs_get(user_id, f"/api/libraries/{lib['id']}/items", params={"limit": 100, "page": page})
            data = resp.json()
            results = data.get("results", [])
            total = data.get("total", len(results))
            for item in results:
                progress = progress_by_item.get(item.get("id"), {})
                book = _item_to_book(item, progress)
                if book:
                    books.append(book)
            page += 1
            if not results or page * 100 >= total:
                break
    return books

# ── StoryGraph ────────────────────────────────────────────────────────────────

# StoryGraph has no public API — these status slugs are reverse-engineered from
# the site's own /update-status.js requests and may change without notice.
_STATUS_LABELS = {
    "to-read": "to read",
    "currently-reading": "currently reading",
    "read": "read",
}

_AUTHOR_SPLIT = re.compile(r"\s*(?:,|&|\band\b)\s*", re.I)
_AUTHOR_JUNK = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}
_LEADING_ARTICLE = re.compile(r"^(the|an|a)\s+")
_PARENS = re.compile(r"\([^)]*\)")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_EDITION_LINK_TEXT = re.compile(r"^\d+\s+editions?$", re.I)
# ABS/Audible often appends "Frontiers Saga Part 2" (and similar) onto the title.
_TRAILING_SERIES = re.compile(
    r"[\s,:\-]+(?:the\s+)?\S+[\s,:\-]+(?:saga|series)"
    r"(?:[\s,:\-]+part\s+\d+)?"
    r"(?:[\s,:\-]+(?:book|episode|ep\.?)\s+\d+)?"
    r"$",
    re.I,
)
_SERIES_ONLY_TITLE = re.compile(
    r"^(?:the\s+)?\S+[\s,:\-]+(?:saga|series)(?:[\s,:\-]+part\s+\d+)?$",
    re.I,
)
_TRAILING_BOOK_NUM = re.compile(
    r"[\s,:\-]+(?:book|episode|ep\.?|vol(?:ume)?|part)\s+\d+$",
    re.I,
)
_TRAILING_SERIES_KIND = re.compile(
    r"[\s,:\-]+(?:the\s+)?(?:saga|series)$",
    re.I,
)


def _normalize_book_title(title: str) -> str:
    t = _PARENS.sub(" ", title or "")
    t = _NON_WORD.sub(" ", t.casefold())
    t = re.sub(r"\s+", " ", t).strip()
    t = _LEADING_ARTICLE.sub("", t)
    return t


def _title_tokens(s: str) -> list[str]:
    s = _NON_WORD.sub(" ", (s or "").casefold())
    return [p for p in s.split() if p]


def _drop_trailing_token_count(title: str, n: int) -> str:
    parts = re.findall(r"\S+", (title or "").strip())
    if n <= 0:
        return (title or "").strip()
    if n >= len(parts):
        return ""
    return " ".join(parts[:-n])


def _contiguous_in(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(hay):
        return False
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _strip_series_from_title(title: str, series_name: str = "") -> str:
    """Drop a trailing series name so StoryGraph search can use the real title.

    ABS often stores 'Rebellion Frontiers Saga Part 2' with seriesName
    'Frontiers Saga, Part 2: Rogue Castes #4'. StoryGraph's title is 'Rebellion'.
    """
    t = (title or "").strip()
    if not t:
        return ""
    series = re.sub(r"#\s*\d+\s*$", "", series_name or "").strip()
    st = _title_tokens(series)
    if st:
        tt = _title_tokens(t)
        for n in range(min(len(tt), len(st)), 1, -1):
            if _contiguous_in(st, tt[-n:]):
                t = _drop_trailing_token_count(t, n)
                break
    trimmed = _TRAILING_SERIES.sub("", t).strip(" ,:-")
    if trimmed:
        t = trimmed
    return t.strip(" ,:-")


def _search_titles(title: str, series_name: str = "", subtitle: str = "") -> list[str]:
    """Title strings to try against StoryGraph browse, best-first."""
    title = (title or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(candidate: str):
        c = (candidate or "").strip(" ,:-")
        key = _normalize_book_title(c)
        if not c or not key or key in seen:
            return
        seen.add(key)
        out.append(c)

    stripped = _strip_series_from_title(title, series_name)
    series_only = (
        not stripped
        or bool(_SERIES_ONLY_TITLE.match(stripped))
        or bool(_SERIES_ONLY_TITLE.match(title))
    )
    if stripped and not _SERIES_ONLY_TITLE.match(stripped):
        add(stripped)
    if series_only and subtitle:
        sub = _TRAILING_BOOK_NUM.sub("", subtitle).strip(" ,:-")
        sub = _TRAILING_SERIES_KIND.sub("", sub).strip(" ,:-")
        add(_strip_series_from_title(sub, series_name))
        add(sub)
    add(title)
    return out


def _titles_compatible(abs_title: str, sg_title: str) -> bool:
    """True when titles are the same work, not a substring of a different title.

    'Roadkill' must not match 'Florida Roadkill'. Extra subtitle words on either
    side are allowed ('Lock In' vs 'Lock In: A Novel').
    """
    a = _normalize_book_title(abs_title)
    s = _normalize_book_title(sg_title)
    if not a or not s:
        return False
    if a == s:
        return True
    return s.startswith(a + " ") or a.startswith(s + " ")


def _author_last_names(author: str) -> list[str]:
    names = []
    for person in _AUTHOR_SPLIT.split(author or ""):
        parts = [
            p for p in _NON_WORD.sub(" ", person.casefold()).split()
            if p and p not in _AUTHOR_JUNK and len(p) > 1
        ]
        if parts:
            names.append(parts[-1])
    return names


def _people_names(value) -> list[str]:
    """ABS people fields: 'A, B', ['A', 'B'], or [{name: 'A'}, ...]."""
    if not value:
        return []
    if isinstance(value, str):
        return [p.strip() for p in _AUTHOR_SPLIT.split(value) if p and p.strip()]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.extend(_people_names(item))
            elif isinstance(item, dict) and item.get("name"):
                out.extend(_people_names(str(item["name"])))
        return out
    return []


def _person_key(name: str) -> str:
    return re.sub(r"\s+", " ", _NON_WORD.sub(" ", (name or "").casefold())).strip()


def _strip_narrators_from_authors(authors: list[str], narrators: list[str]) -> list[str]:
    narr = {_person_key(n) for n in narrators if _person_key(n)}
    if not narr:
        return authors
    kept = [a for a in authors if _person_key(a) not in narr]
    return kept or authors


def _search_authors(author: str) -> list[str]:
    """Author strings to try in StoryGraph browse, best-first, ending with none."""
    author = (author or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(candidate: str):
        c = (candidate or "").strip()
        key = c.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(c)

    if author:
        add(author)
        people = _people_names(author)
        if people:
            add(people[0])
    add("")
    return out


def _authors_compatible(abs_author: str, sg_author: str) -> bool:
    if not (abs_author or "").strip():
        return True
    if not (sg_author or "").strip():
        return False
    sg = sg_author.casefold()
    lasts = _author_last_names(abs_author)
    if not lasts:
        return abs_author.casefold() in sg
    return any(re.search(rf"\b{re.escape(last)}\b", sg) for last in lasts)


def _book_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"/books/([^/?]+)", href)
    return m.group(1) if m else None


def _pane_title_author_id(pane) -> tuple[str, str, str | None]:
    book_id = pane.get("data-book-id") or None
    title = ""
    block = pane.select_one(".book-title-author-and-series")
    if block:
        h3a = block.select_one("h3 a[href*='/books/']")
        if h3a:
            title = h3a.get_text(" ", strip=True)
            book_id = book_id or _book_id_from_href(h3a.get("href"))
    if not title:
        for a in pane.select("a[href*='/books/']"):
            href = a.get("href") or ""
            if "/editions" in href:
                continue
            txt = a.get_text(" ", strip=True)
            if txt and not _EDITION_LINK_TEXT.match(txt):
                title = txt
                book_id = book_id or _book_id_from_href(href)
                break
    author_el = pane.select_one("a[href*='/authors/']")
    author = author_el.get_text(" ", strip=True) if author_el else ""
    if not book_id:
        link = pane.find("a", href=re.compile(r"/books/[0-9a-f-]{8,}"))
        book_id = _book_id_from_href(link.get("href") if link else None)
    return title, author, book_id


def _match_titles(title: str, alt_titles: list | None = None) -> list[str]:
    out = []
    seen = set()
    for t in [title, *(alt_titles or [])]:
        t = (t or "").strip()
        key = _normalize_book_title(t)
        if t and key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _pick_book_id_from_search(
    soup,
    title: str,
    author: str = "",
    identifiers: list | None = None,
    alt_titles: list | None = None,
) -> str | None:
    """Pick a StoryGraph work from browse results, or None if nothing is safe.

    Fail-closed: require an ASIN/ISBN hit, or compatible title AND author.
    Title-only hits are never accepted when an author is known. Without an
    author or identifier, only a single exact-title candidate is allowed.
    """
    identifiers = [i.strip() for i in (identifiers or []) if i and str(i).strip()]
    idents_l = [i.casefold() for i in identifiers]
    titles = _match_titles(title, alt_titles)
    author = (author or "").strip()
    panes = soup.select("div.book-pane")
    candidates = []
    for pane in panes:
        sg_title, sg_author, book_id = _pane_title_author_id(pane)
        if not book_id:
            continue
        ctx = " ".join(pane.get_text(" ", strip=True).split())
        ctx_l = ctx.casefold()
        ident_hit = any(i in ctx_l for i in idents_l)
        title_ok = any(_titles_compatible(t, sg_title) for t in titles)
        author_ok = bool(author) and bool(sg_author) and _authors_compatible(author, sg_author)
        # Identifiers alone are enough; otherwise both title and author must agree.
        if ident_hit:
            pass
        elif author:
            if not (title_ok and author_ok):
                continue
        else:
            # No author and no identifier: exact title only, disambiguated later.
            if not title_ok:
                continue
        exact = any(_normalize_book_title(t) == _normalize_book_title(sg_title) for t in titles)
        score = 0
        if ident_hit:
            score += 100
        score += 50 if exact else 25
        if author_ok:
            score += 40
        candidates.append((score, exact, book_id, sg_title, sg_author, ident_hit, author_ok))

    if not panes:
        link = soup.find("a", class_="book-title-link")
        if not link:
            container = soup.find(class_="book-title-author-and-series")
            if container:
                link = container.find("a", href=re.compile(r"^/books/"))
        if link:
            sg_title = link.get_text(" ", strip=True)
            parent_txt = " ".join((link.parent.get_text(" ", strip=True) if link.parent else sg_title).split())
            book_id = _book_id_from_href(link.get("href"))
            ident_hit = any(i in parent_txt.casefold() for i in idents_l)
            title_ok = any(_titles_compatible(t, sg_title) for t in titles)
            author_ok = bool(author) and _authors_compatible(author, parent_txt)
            if book_id and (ident_hit or (title_ok and (author_ok or not author))):
                if author and not ident_hit and not author_ok:
                    return None
                if not author and not ident_hit:
                    # Single-result page with no author: only exact title.
                    if not any(_normalize_book_title(t) == _normalize_book_title(sg_title) for t in titles):
                        return None
                return book_id
        return None

    if not candidates:
        return None

    # Drop title-only soft matches when we have an author; keep ident / author hits.
    if author:
        strong = [c for c in candidates if c[5] or c[6]]  # ident_hit or author_ok
        if not strong:
            return None
        candidates = strong
    else:
        # No author: require a unique exact title (or a unique ident hit).
        exact_ids = {c[2] for c in candidates if c[1] or c[5]}
        if len(exact_ids) != 1:
            logger.warning(
                "Ambiguous StoryGraph match for '%s' (no author, %d candidates), skipping",
                title, len({c[2] for c in candidates}),
            )
            return None
        return next(iter(exact_ids))

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0]
    # Refuse near-ties across different works.
    tied = [c for c in candidates if c[0] == best[0] and c[2] != best[2]]
    if tied:
        logger.warning(
            "Ambiguous StoryGraph match for '%s' (tied score %s), skipping",
            title, best[0],
        )
        return None
    # Soft title+author (non-exact, no ident) still needs the author bonus.
    if not best[5] and not best[1] and best[0] < 65:
        logger.warning(
            "Weak StoryGraph match for '%s' (score=%s), skipping",
            title, best[0],
        )
        return None
    return best[2]


class StoryGraphClient:
    def __init__(self, session_cookie: str, remember_token: str = ""):
        self._session = req.Session()
        for name, val in [
            ("_storygraph_session", session_cookie),
            ("remember_user_token", remember_token),
            ("cookies_popup_seen", "yes"),
            ("plus_popup_seen", "yes"),
        ]:
            self._session.cookies.set(name, val, domain="app.thestorygraph.com")
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "Accept-Language": "en",
            "Origin": STORYGRAPH_BASE,
        })
        self._last_csrf = None

    def _extract_csrf(self, html):
        m = (
            re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', html)
            or re.search(r'<input[^>]+name=["\']authenticity_token["\'][^>]+value=["\']([^"\']+)["\']', html)
        )
        if m:
            self._last_csrf = m.group(1)
        return self._last_csrf

    def _get(self, path):
        resp = self._session.get(f"{STORYGRAPH_BASE}{path}", timeout=15)
        self._extract_csrf(resp.text)
        if "_storygraph_session" in resp.cookies:
            self._session.cookies.set("_storygraph_session", resp.cookies["_storygraph_session"], domain="app.thestorygraph.com")
        return resp

    def _post(self, path, data):
        return self._session.post(
            f"{STORYGRAPH_BASE}{path}", data=data,
            headers={
                "X-CSRF-Token": self._last_csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "text/javascript, application/javascript, */*; q=0.01",
                "Referer": STORYGRAPH_BASE,
            },
            allow_redirects=False, timeout=15,
        )

    def check_auth(self) -> bool:
        resp = self._get("/")
        return "sign_in" not in resp.url

    def search_book(
        self,
        title,
        author,
        prefer_audio: bool = False,
        narrators: list | None = None,
        identifiers: list | None = None,
        series_name: str = "",
        subtitle: str = "",
    ) -> str | None:
        # Identifiers (ASIN/ISBN) are used to score panes, not as search text.
        # StoryGraph browse returns no book-panes for Audible ASINs like B0B6QBNK4J.
        # ABS titles often include the series ("Rebellion Frontiers Saga Part 2");
        # StoryGraph has nothing for that string, so retry with the core title.
        # authorName often includes the narrator ("Andy Weir, Ray Porter"); that
        # extra name also makes browse return zero hits, so retry a shorter author.
        queries = _search_titles(title, series_name=series_name, subtitle=subtitle)
        author_queries = _search_authors(author)
        book_id = None
        for qtitle in queries:
            for qauthor in author_queries:
                query = req.utils.quote(f"{qtitle} {qauthor}".strip())
                resp = self._get(f"/browse?search_term={query}")
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                book_id = _pick_book_id_from_search(
                    soup, title, author or "",
                    identifiers=identifiers,
                    alt_titles=queries,
                )
                if book_id:
                    break
            if book_id:
                break
        if not book_id:
            logger.warning("No StoryGraph result for '%s'", title)
            return None
        chosen = book_id
        if prefer_audio:
            chosen = self._prefer_audio_edition(book_id, narrators=narrators or []) or book_id
        logger.info("Found '%s' -> id=%s", title, chosen)
        return chosen

    def _prefer_audio_edition(self, book_id: str, narrators: list | None = None) -> str | None:
        """Prefer an Audio edition of this work, matching ABS narrator when possible."""
        narrators_l = [n.casefold() for n in (narrators or []) if n]
        try:
            html = self._get(f"/books/{book_id}/editions").text
        except Exception:
            return book_id
        soup = BeautifulSoup(html, "html.parser")
        audio_ids = []
        for pane in soup.select("div.book-pane"):
            txt = " ".join(pane.get_text(" ", strip=True).split())
            if not re.search(r"Format:\s*Audio\b", txt, re.I):
                continue
            link = pane.find("a", href=re.compile(r"/books/[0-9a-f-]{8,}"))
            if not link:
                continue
            m = re.search(r"/books/([^/?]+)", link.get("href", ""))
            if not m:
                continue
            bid = m.group(1)
            txt_l = txt.casefold()
            narrator_hit = any(n in txt_l for n in narrators_l)
            audio_ids.append((2 if narrator_hit else 1, bid))
        if not audio_ids:
            return book_id
        audio_ids.sort(key=lambda x: x[0], reverse=True)
        return audio_ids[0][1]

    def switch_edition_if_needed(self, to_book_id: str, html: str | None = None) -> tuple[bool, str | None]:
        """If another edition holds the read status, POST /switch-editions to move it.
        Returns (ok, html) — html is reusable page markup, or None when stale after a switch."""
        page = html if html is not None else self.get_book_page(to_book_id)
        if "currently reading another edition" not in page.casefold():
            return True, page
        try:
            editions_html = self._get(f"/books/{to_book_id}/editions").text
        except Exception:
            return False, page
        soup = BeautifulSoup(editions_html, "html.parser")
        for form in soup.find_all("form", action=re.compile(r"/switch-editions")):
            fields = {
                inp.get("name"): inp.get("value")
                for inp in form.find_all("input")
                if inp.get("name")
            }
            if fields.get("to_book_id") != to_book_id:
                continue
            from_id = fields.get("from_book_id")
            if not from_id:
                continue
            r = self._post(
                "/switch-editions",
                {
                    "authenticity_token": fields.get("authenticity_token") or self._last_csrf,
                    "from_book_id": from_id,
                    "to_book_id": to_book_id,
                },
            )
            logger.info(
                "Switched edition %s -> %s: HTTP %s",
                from_id[:8], to_book_id[:8], r.status_code,
            )
            return r.status_code in (200, 302), None
        logger.warning("No /switch-editions form targeting %s", to_book_id[:8])
        return False, page

    def get_book_page(self, book_id) -> str:
        return self._get(f"/books/{book_id}").text

    def _parse_current_progress(self, html) -> float | None:
        """Best-effort read of the percentage StoryGraph currently has on file for
        this book, from the same hidden field update_progress() fills in. Returns
        None if it can't be found — callers should treat that as 'unknown' and not
        skip a write on its account."""
        m = re.search(
            r'(?:name="read_status\[progress_number\]"|class="[^"]*\bread-status-progress-number\b[^"]*")[^>]*value="([^"]*)"'
            r'|value="([^"]*)"[^>]*(?:name="read_status\[progress_number\]"|class="[^"]*\bread-status-progress-number\b[^"]*")',
            html,
        )
        raw = (m.group(1) or m.group(2)) if m else None
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def ensure_status(self, book_id, target_status, html: str | None = None) -> tuple[bool, bool, str | None]:
        """Returns (ok, already_matched, html) — already_matched is True when the book's
        StoryGraph status already matched target_status and nothing was posted. html is
        the page markup the match was read from (reusable for a progress check), or None
        if a status-changing POST was made and any previously-fetched markup is now stale."""
        html = html if html is not None else self.get_book_page(book_id)
        m = re.search(r'class="[^"]*\bread-status-label\b[^"]*"[^>]*>([^<]*)<', html, re.I)
        current = m.group(1).strip().lower() if m else ""
        label = _STATUS_LABELS[target_status]
        if label in current or (target_status == "currently-reading" and "rereading" in current):
            return True, True, html
        r = self._post(
            f"/update-status.js?book_id={book_id}&status={target_status}",
            {"authenticity_token": self._last_csrf},
        )
        logger.info("Set status=%s for %s: HTTP %s", target_status, book_id, r.status_code)
        return r.status_code in (200, 302), False, None

    def ensure_finish_date(
        self,
        book_id: str,
        finished_at,
        started_at=None,
        html: str | None = None,
    ) -> tuple[bool, str | None]:
        """Set StoryGraph finish (and optional start) date from ABS timestamps.
        Returns (ok, ymd) where ymd is YYYY-MM-DD of the finish date written/confirmed."""
        finish = _abs_ts_to_ymd(finished_at)
        if not finish:
            return True, None
        f_day, f_month, f_year = finish
        finish_ymd = f"{f_year:04d}-{f_month:02d}-{f_day:02d}"
        start = _abs_ts_to_ymd(started_at)
        page = html if html is not None else self.get_book_page(book_id)
        link = re.search(r'href="(/edit-read-instance-from-book\?[^"]+)"', page, re.I)
        if not link:
            logger.warning("No edit-read-instance link for %s", book_id)
            return False, None
        path = unquote(html_lib.unescape(link.group(1)))
        form_html = self._get(path).text
        instance_m = re.search(r'name="read_instance_id"[^>]*value="([^"]+)"', form_html) or re.search(
            r'value="([^"]+)"[^>]*name="read_instance_id"', form_html
        )
        if not instance_m:
            logger.warning("No read_instance_id on edit form for %s", book_id)
            return False, None
        instance_id = instance_m.group(1)

        def _selected(name: str) -> str:
            msel = re.search(
                rf'<select[^>]*name="{re.escape(name)}"[^>]*>(.*?)</select>',
                form_html,
                flags=re.I | re.S,
            )
            if not msel:
                return ""
            sm = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', msel.group(1), re.I) or re.search(
                r'<option[^>]*value="([^"]*)"[^>]*selected', msel.group(1), re.I
            )
            return sm.group(1) if sm else ""

        existing = (
            _selected("read_instance[year]"),
            _selected("read_instance[month]"),
            _selected("read_instance[day]"),
        )
        if existing == (str(f_year), str(f_month), str(f_day)):
            return True, finish_ymd

        payload = {
            "_method": "patch",
            "authenticity_token": self._last_csrf,
            "book_id": book_id,
            "read_instance_id": instance_id,
            "read_instance[day]": str(f_day),
            "read_instance[month]": str(f_month),
            "read_instance[year]": str(f_year),
            "read_instance[start_day]": str(start[0]) if start else "",
            "read_instance[start_month]": str(start[1]) if start else "",
            "read_instance[start_year]": str(start[2]) if start else "",
            "commit": "Update",
        }
        r = self._session.post(
            f"{STORYGRAPH_BASE}/read_instances/{instance_id}",
            data=payload,
            headers={
                "X-CSRF-Token": self._last_csrf,
                "Referer": f"{STORYGRAPH_BASE}{path}",
                "Origin": STORYGRAPH_BASE,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            allow_redirects=True,
            timeout=15,
        )
        # StoryGraph sometimes answers with an odd status; confirm via the edit form.
        confirm_html = self._get(path).text

        def _selected_after(name: str) -> str:
            msel = re.search(
                rf'<select[^>]*name="{re.escape(name)}"[^>]*>(.*?)</select>',
                confirm_html,
                flags=re.I | re.S,
            )
            if not msel:
                return ""
            sm = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', msel.group(1), re.I) or re.search(
                r'<option[^>]*value="([^"]*)"[^>]*selected', msel.group(1), re.I
            )
            return sm.group(1) if sm else ""

        confirmed = (
            _selected_after("read_instance[year]"),
            _selected_after("read_instance[month]"),
            _selected_after("read_instance[day]"),
        ) == (str(f_year), str(f_month), str(f_day))
        ok = confirmed or r.status_code in (200, 302, 303)
        logger.info(
            "Finish date for %s -> %s: HTTP %s confirmed=%s",
            book_id, finish_ymd, r.status_code, confirmed,
        )
        return ok, finish_ymd if ok else None

    def update_progress(self, book_id, progress_percent, html: str | None = None) -> bool:
        html = html if html is not None else self.get_book_page(book_id)
        m = re.search(
            r'(?:name="read_status\[book_num_of_pages\]"|class="[^"]*\bread-status-book-num-of-pages\b[^"]*")[^>]*value="([^"]*)"'
            r'|value="([^"]*)"[^>]*(?:name="read_status\[book_num_of_pages\]"|class="[^"]*\bread-status-book-num-of-pages\b[^"]*")',
            html,
        )
        book_pages = (m.group(1) or m.group(2)) if m else "0"
        r = self._post("/update-progress", {
            "read_status[progress_number]": str(round(progress_percent, 1)),
            "read_status[progress_type]": "percentage",
            "read_status[book_num_of_pages]": book_pages or "0",
            "book_id": book_id,
            "on_book_page": "true",
            "authenticity_token": self._last_csrf,
        })
        ok = r.status_code in (200, 302)
        logger.info("Progress for %s -> %.1f%%: HTTP %s", book_id, progress_percent, r.status_code)
        return ok


def _abs_ts_to_ymd(ts) -> tuple[int, int, int] | None:
    if ts is None or ts == "":
        return None
    try:
        n = float(ts)
        if n > 1e12:
            n /= 1000.0
        dt = datetime.fromtimestamp(n, timezone.utc)
        return dt.day, dt.month, dt.year
    except Exception:
        return None

# ── Sync logic ────────────────────────────────────────────────────────────────

_last_synced: dict[str, dict[str, float]] = {}
_last_synced_lock = threading.Lock()

_status_cache: dict[str, dict] = {}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 60  # seconds


def get_cached_books(user_id: str, scope: str) -> tuple[list[dict], bool]:
    with _status_cache_lock:
        cache = _status_cache.get(user_id, {"books": [], "abs_ok": False, "ts": 0.0})
        if time.time() - cache["ts"] < STATUS_CACHE_TTL:
            return cache["books"], cache["abs_ok"]
    try:
        books = get_abs_books(user_id, scope)
        with _status_cache_lock:
            _status_cache[user_id] = {"books": books, "abs_ok": True, "ts": time.time()}
        return books, True
    except Exception:
        return cache["books"], False


def _target_status(book: dict) -> str:
    if book.get("is_finished"):
        return "read"
    if book["progress_percent"] > 0:
        return "currently-reading"
    return "to-read"


def do_sync(user_id: str, books: list[dict]) -> list[dict]:
    user = get_user(user_id)
    label = (user or {}).get("username") or (user or {}).get("display_name") or user_id
    client = StoryGraphClient(cfg(user_id, "STORYGRAPH_SESSION"), cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN"))
    if not client.check_auth():
        logger.error("[%s] StoryGraph session invalid — update STORYGRAPH_SESSION", label)
        return [{"title": b["title"], "status": "auth_error"} for b in books]
    synced = _get_synced(user_id)
    results = []
    for book in books:
        try:
            pct = book["progress_percent"]
            status = _target_status(book)
            prev = synced.get(book["title"])
            finish_parts = _abs_ts_to_ymd(book.get("finished_at")) if book.get("is_finished") else None
            finish_ymd = (
                f"{finish_parts[2]:04d}-{finish_parts[1]:02d}-{finish_parts[0]:02d}"
                if finish_parts else None
            )
            needs_finish_date = bool(
                status == "read"
                and finish_ymd
                and (prev or {}).get("finished_ymd") != finish_ymd
            )
            # Audiobooks without a stored StoryGraph edition id must re-run search
            # so we can migrate off a previously synced print edition.
            edition_stale = bool(book.get("prefer_audio")) and not (prev or {}).get("book_id")
            status_unchanged = (
                prev is not None
                and prev.get("status") == status
                and abs(pct - prev.get("pct", -1)) < 0.5
            )
            if status_unchanged and not needs_finish_date and not edition_stale:
                logger.info("[%s] '%s' unchanged (%s, %.1f%%) — skipping", label, book["title"], status, pct)
                results.append({"title": book["title"], "status": "unchanged", "progress_percent": pct})
                continue

            book_id = client.search_book(
                book["title"],
                book["author"],
                prefer_audio=bool(book.get("prefer_audio")),
                narrators=book.get("narrators") or [],
                identifiers=[i for i in (book.get("asin"), book.get("isbn")) if i],
                series_name=book.get("series_name") or "",
                subtitle=book.get("subtitle") or "",
            )
            if not book_id:
                results.append({"title": book["title"], "status": "not_found"})
                continue
            switched_ok, status_html = client.switch_edition_if_needed(book_id)
            if not switched_ok:
                results.append({"title": book["title"], "status": "edition_switch_failed"})
                continue
            ok, already_matched, status_html = client.ensure_status(book_id, status, html=status_html)
            target_pct = 100 if status == "read" else pct
            # Skip the progress POST when StoryGraph already agrees with us. "read" is
            # always 100% by definition, so an already-matched read status is always
            # safe to skip (this was the original fix for finished-book duplicates).
            # For "currently reading" we can't assume that — the status label matching
            # doesn't tell us the percentage matches too — so we additionally parse the
            # progress StoryGraph has on file and only skip if it's already within
            # rounding distance of what we'd post. Without this, a book that already
            # matched (e.g. every "currently reading" book on a fresh install, before
            # this tool has any local sync-state for it) still got a progress POST on
            # every single poll, creating a spurious "started" entry and a duplicate
            # progress update in the StoryGraph journal even though nothing about the
            # book had actually changed. If we can't parse the current percentage at
            # all, we conservatively still post — no worse than the old behavior.
            current_pct = client._parse_current_progress(status_html or "") if already_matched else None
            skip_progress = status == "to-read" or (
                already_matched
                and (status == "read" or (current_pct is not None and abs(current_pct - target_pct) < 0.5))
            )
            if not skip_progress:
                ok = client.update_progress(book_id, target_pct, html=status_html)
            if ok and needs_finish_date:
                date_ok, written_ymd = client.ensure_finish_date(
                    book_id,
                    book.get("finished_at"),
                    started_at=book.get("started_at"),
                    html=status_html,
                )
                ok = ok and date_ok
                if date_ok and written_ymd:
                    finish_ymd = written_ymd
            if ok:
                entry = {"pct": pct, "status": status, "book_id": book_id}
                if finish_ymd:
                    entry["finished_ymd"] = finish_ymd
                elif prev and prev.get("finished_ymd"):
                    entry["finished_ymd"] = prev["finished_ymd"]
                synced[book["title"]] = entry
                _save_sync_state(user_id, synced)
            results.append({
                "title": book["title"],
                "status": "success" if ok else "failed",
                "progress_percent": pct,
                "current_minutes": book["current_minutes"],
            })
        except Exception as e:
            logger.error("[%s] Error syncing '%s': %s", label, book["title"], e)
            results.append({"title": book["title"], "status": "error", "error": str(e)})
    return results


def _poll_user(user: dict):
    user_id = user["id"]
    if not all([cfg(user_id, "ABS_URL"), cfg(user_id, "ABS_TOKEN"), cfg(user_id, "STORYGRAPH_SESSION")]):
        return
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    label = user.get("username") or user.get("display_name") or user_id
    try:
        books = get_abs_books(user_id, scope)
        with _status_cache_lock:
            _status_cache[user_id] = {"books": books, "abs_ok": True, "ts": time.time()}
        to_sync = []
        with _last_synced_lock:
            last = _last_synced.setdefault(user_id, {})
            for b in books:
                prev = last.get(b["title"], 0.0)
                if b["is_finished"] or b["current_minutes"] - prev >= SYNC_THRESHOLD:
                    to_sync.append(b)
        if to_sync:
            results = do_sync(user_id, to_sync)
            with _last_synced_lock:
                for b in to_sync:
                    _last_synced[user_id][b["title"]] = b["current_minutes"]
            synced = sum(1 for r in results if r["status"] == "success")
            logger.info("[%s] Auto-sync: %d/%d synced", label, synced, len(to_sync))
    except Exception as e:
        logger.error("[%s] Auto-sync error: %s", label, e)


def _poll_loop():
    logger.info("Auto-sync started: polling every %ds, threshold %.1f min", POLL_INTERVAL, SYNC_THRESHOLD)
    while True:
        time.sleep(POLL_INTERVAL)
        for user in list_users():
            _poll_user(user)

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = _get_secret_key()

# Trust one hop of X-Forwarded-Proto/Host/For/Prefix from a reverse proxy in
# front of the container (Caddy, nginx, Traefik, ...). Without this, Flask has
# no way to know the original request came in over https when the proxy talks
# plain http to the container — so url_for(..., _external=True) (used to build
# the OIDC redirect_uri sent to the provider) generates an http:// URL even
# behind an https-terminating proxy, which providers like Google reject as a
# redirect_uri mismatch. Setting the proxy's forwarded headers alone doesn't
# fix this on its own — Flask/Werkzeug ignore them unless told to trust them,
# which is what this does.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

oauth = OAuth(app)
if OIDC_ENABLED:
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

PUBLIC_ENDPOINTS = {"login", "login_oidc", "auth_callback", "logout", "setup", "static"}


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user or not g.user.get("is_admin"):
            return jsonify({"error": "Forbidden"}), 403
        return view(*args, **kwargs)
    return wrapped


@app.before_request
def load_user():
    g.user = None
    if request.endpoint == "static":
        return
    user_id = session.get("user_id")
    if user_id:
        g.user = get_user(user_id)
        if g.user is None:
            session.clear()
    if not any_users_exist():
        if request.endpoint != "setup":
            return redirect(url_for("setup"))
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if g.user is None:
        return redirect(url_for("login"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if any_users_exist():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Username and password are required."
        else:
            user = create_user(username=username, password=password, is_admin=True)
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
    return render_template("setup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        user = get_user_by_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        if user and user.get("password_hash") and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error, oidc_enabled=OIDC_ENABLED)


@app.route("/login/oidc")
def login_oidc():
    if not OIDC_ENABLED:
        return redirect(url_for("login"))
    redirect_uri = f"{PUBLIC_URL}/auth/callback" if PUBLIC_URL else url_for("auth_callback", _external=True)
    return oauth.oidc.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    if not OIDC_ENABLED:
        return redirect(url_for("login"))
    token = oauth.oidc.authorize_access_token()
    claims = token.get("userinfo") or oauth.oidc.userinfo(token=token)
    sub = claims.get("sub")
    if not sub:
        return redirect(url_for("login"))
    user = get_user_by_oidc_sub(sub)
    if not user:
        display_name = claims.get("preferred_username") or claims.get("email") or sub
        user = create_user(oidc_sub=sub, display_name=display_name, is_admin=False)
    session["user_id"] = user["id"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template(
        "index.html",
        is_admin=bool(g.user.get("is_admin")),
        display_name=g.user.get("display_name") or g.user.get("username"),
    )


@app.route("/api/status")
def api_status():
    user_id = g.user["id"]
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    books, abs_ok = [], False
    if cfg(user_id, "ABS_URL") and cfg(user_id, "ABS_TOKEN"):
        books, abs_ok = get_cached_books(user_id, scope)
    with _last_synced_lock:
        last = dict(_last_synced.get(user_id, {}))
    return jsonify({
        "abs_ok": abs_ok,
        "sg_ok": bool(cfg(user_id, "STORYGRAPH_SESSION")),
        "auto_sync": True,
        "poll_interval": POLL_INTERVAL,
        "sync_threshold": SYNC_THRESHOLD,
        "sync_scope": scope,
        "books": books,
        "last_synced": last,
    })


@app.route("/api/sync", methods=["POST"])
def api_sync():
    user_id = g.user["id"]
    missing = [k for k in ("ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION") if not cfg(user_id, k)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 500
    scope = cfg(user_id, "SYNC_SCOPE", "in_progress")
    try:
        books = get_abs_books(user_id, scope)
        if not books:
            return jsonify({"message": "No books found", "synced": 0, "total": 0, "results": []})
        results = do_sync(user_id, books)
        with _last_synced_lock:
            last = _last_synced.setdefault(user_id, {})
            for b in books:
                last[b["title"]] = b["current_minutes"]
        synced = sum(1 for r in results if r["status"] == "success")
        return jsonify({"message": "Sync complete", "synced": synced, "total": len(books), "results": results})
    except Exception as e:
        logger.error("Sync failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs")
@admin_required
def api_logs():
    return jsonify({"logs": _log_buffer.get()})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    user_id = g.user["id"]
    data = request.json or {}
    allowed = {"ABS_URL", "ABS_TOKEN", "STORYGRAPH_SESSION", "STORYGRAPH_REMEMBER_TOKEN", "SYNC_SCOPE"}
    if "ABS_URL" in data and data["ABS_URL"]:
        parsed = urlparse(data["ABS_URL"])
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "ABS_URL must use http or https"}), 400
        if not parsed.hostname:
            return jsonify({"error": "ABS_URL must include a hostname"}), 400
    if data.get("SYNC_SCOPE") and data["SYNC_SCOPE"] not in SYNC_SCOPES:
        return jsonify({"error": "Invalid SYNC_SCOPE"}), 400
    set_cfg(user_id, {k: v for k, v in data.items() if k in allowed})
    logger.info("[%s] Settings updated via UI", g.user.get("username") or g.user.get("display_name") or user_id)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current config keys (masked values) so the UI can show what's set."""
    user_id = g.user["id"]
    return jsonify({
        "ABS_URL": cfg(user_id, "ABS_URL"),
        "ABS_TOKEN": "set" if cfg(user_id, "ABS_TOKEN") else "",
        "STORYGRAPH_SESSION": "set" if cfg(user_id, "STORYGRAPH_SESSION") else "",
        "STORYGRAPH_REMEMBER_TOKEN": "set" if cfg(user_id, "STORYGRAPH_REMEMBER_TOKEN") else "",
        "SYNC_SCOPE": cfg(user_id, "SYNC_SCOPE", "in_progress"),
    })


@app.route("/api/users")
@admin_required
def api_users():
    users = [{
        "id": u["id"],
        "username": u.get("username"),
        "display_name": u.get("display_name"),
        "is_admin": bool(u.get("is_admin")),
        "via_oidc": bool(u.get("oidc_sub")),
    } for u in list_users()]
    return jsonify({"users": users})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "Username already taken"}), 400
    user = create_user(username=username, password=password, is_admin=bool(data.get("is_admin")))
    return jsonify({"id": user["id"]})


@app.route("/api/users/<user_id>", methods=["DELETE"])
@admin_required
def api_delete_user(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot delete your own account"}), 400
    delete_user(user_id)
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>/admin", methods=["POST"])
@admin_required
def api_set_admin(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "Cannot change your own admin status"}), 400
    data = request.json or {}
    update_user(user_id, is_admin=bool(data.get("is_admin")))
    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
