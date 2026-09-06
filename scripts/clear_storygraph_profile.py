#!/usr/bin/env python3
"""Remove every book from a StoryGraph profile.

Uses abs-storygraph-sync session cookies.
POST /remove-book/{id}?remove_tags=true

Usage:
  sudo python3 clear_storygraph_profile.py [--dry-run] [--keep-tags] [--passes N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://app.thestorygraph.com"
DEFAULT_CFG = "/opt/_dockers/abs-storygraph-sync/data/config/1ac26ce0e909448fb33e8ed165ff0fdb.json"
DEFAULT_STATE = "/opt/_dockers/abs-storygraph-sync/data/sync_state/1ac26ce0e909448fb33e8ed165ff0fdb.json"

# (path template with {uid} or {user}, query extras)
LISTS = (
    ("books-read/{uid}", "year=0&per_page=100"),
    ("currently-reading/{uid}", "per_page=100"),
    ("to-read/{uid}", "per_page=100"),
    ("books-dnf/{user}", "per_page=100"),
    ("owned-books/{user}", "per_page=100"),
    ("books-read/{user}", "year=0&per_page=100"),
    ("currently-reading/{user}", "per_page=100"),
    ("to-read/{user}", "per_page=100"),
)


def _csrf(html: str) -> str | None:
    m = (
        re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']', html)
        or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']', html)
    )
    return m.group(1) if m else None


def _client(session_cookie: str, remember: str) -> requests.Session:
    s = requests.Session()
    for name, val in (
        ("_storygraph_session", session_cookie),
        ("remember_user_token", remember),
        ("cookies_popup_seen", "yes"),
        ("plus_popup_seen", "yes"),
    ):
        if val:
            s.cookies.set(name, val, domain="app.thestorygraph.com")
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en",
        "Origin": BASE,
    })
    return s


def _discover(s: requests.Session) -> tuple[str, str]:
    r = s.get(f"{BASE}/", timeout=30)
    r.raise_for_status()
    user = None
    m = re.search(r'href="/(?:profile|to-read|currently-reading|books-read)/([^"/?]+)"', r.text)
    if m:
        user = m.group(1)
    # UUID from books-read form / select user_id attr
    uid = None
    m = re.search(r'action="/books-read/([0-9a-f-]{36})"', r.text)
    if m:
        uid = m.group(1)
    if not uid:
        br = s.get(f"{BASE}/books-read/{user}", timeout=45)
        m = re.search(r'action="/books-read/([0-9a-f-]{36})"', br.text)
        if m:
            uid = m.group(1)
        if not uid:
            m = re.search(r'user_id=["\']([0-9a-f-]{36})["\']', br.text)
            if m:
                uid = m.group(1)
    if not user or not uid:
        raise SystemExit(f"Could not discover username/uid (user={user!r} uid={uid!r})")
    return user, uid


def _ids_from_html(html: str) -> set[str]:
    ids = set()
    soup = BeautifulSoup(html, "html.parser")
    for p in soup.select("div.book-pane[data-book-id]"):
        ids.add(p["data-book-id"])
    ids |= set(re.findall(r'data-remove-path="/remove-book/([0-9a-f-]{8,})"', html, re.I))
    return ids


def _paginate(s: requests.Session, path: str, query: str) -> set[str]:
    found: set[str] = set()
    for page in range(1, 80):
        q = query
        if page > 1:
            q = f"{query}&page={page}" if query else f"page={page}"
        url = f"{BASE}/{path}"
        if q:
            url += f"?{q}"
        r = s.get(url, timeout=90)
        if r.status_code == 404:
            break
        r.raise_for_status()
        batch = _ids_from_html(r.text)
        new = batch - found
        print(f"  {path} p{page}: {len(batch)} ids, {len(new)} new", flush=True)
        if not new:
            break
        found |= batch
        time.sleep(0.25)
    return found


def collect_ids(s: requests.Session, user: str, uid: str, state_path: str | None) -> set[str]:
    ids: set[str] = set()
    if state_path:
        try:
            st = json.load(open(state_path, encoding="utf-8"))
            for v in st.values():
                bid = (v or {}).get("book_id")
                if bid:
                    ids.add(bid)
            print(f"from sync_state: {len(ids)}", flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"sync_state skip: {e}", flush=True)

    for tmpl, query in LISTS:
        path = tmpl.format(user=user, uid=uid)
        print(f"listing {path}…", flush=True)
        before = len(ids)
        ids |= _paginate(s, path, query)
        print(f"  total after: {len(ids)} (+{len(ids) - before})", flush=True)
    return ids


def remove_book(s: requests.Session, book_id: str, csrf: str, remove_tags: bool) -> tuple[bool, int]:
    url = f"{BASE}/remove-book/{book_id}?remove_tags={'true' if remove_tags else 'false'}"
    r = s.post(
        url,
        headers={
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/javascript, application/javascript, */*; q=0.01",
            "Referer": BASE,
        },
        timeout=30,
    )
    if "_storygraph_session" in r.cookies:
        s.cookies.set(
            "_storygraph_session",
            r.cookies["_storygraph_session"],
            domain="app.thestorygraph.com",
        )
    return r.status_code in (200, 302), r.status_code


def remove_owned(s: requests.Session, book_id: str, csrf_token: str) -> tuple[bool, int]:
    r = s.request(
        "DELETE",
        f"{BASE}/remove-owned-book?book_id={book_id}",
        headers={
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/javascript, application/javascript, */*; q=0.01",
            "Referer": f"{BASE}/books/{book_id}",
        },
        timeout=30,
    )
    return r.status_code in (200, 302), r.status_code


def collect_owned_ids(s: requests.Session, user: str) -> set[str]:
    return _paginate(s, f"owned-books/{user}", "per_page=100")


def profile_counts(s: requests.Session, user: str) -> str:
    r = s.get(f"{BASE}/profile/{user}", timeout=45)
    text = " ".join(BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True).split())
    bits = []
    for label in ("Currently Reading", "To-Read Pile", "Owned", "Did Not Finish", "Read Books"):
        m = re.search(rf"{re.escape(label)}\s*\(?(\d+)\)?", text)
        if m:
            bits.append(f"{label}={m.group(1)}")
    return ", ".join(bits) or "(counts not parsed)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=DEFAULT_CFG)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-tags", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--passes", type=int, default=3, help="Re-scrape and remove leftover books")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    session_cookie = cfg.get("STORYGRAPH_SESSION") or ""
    remember = cfg.get("STORYGRAPH_REMEMBER_TOKEN") or ""
    if not session_cookie:
        print("No STORYGRAPH_SESSION in config", file=sys.stderr)
        return 1

    s = _client(session_cookie, remember)
    home = s.get(f"{BASE}/", timeout=30)
    if "sign_in" in home.url:
        print("StoryGraph session invalid", file=sys.stderr)
        return 1
    csrf = _csrf(home.text)
    if not csrf:
        print("No CSRF token", file=sys.stderr)
        return 1

    user, uid = _discover(s)
    print(f"username={user} uid={uid}", flush=True)
    print(f"profile before: {profile_counts(s, user)}", flush=True)

    remove_tags = not args.keep_tags
    total_ok = total_fail = 0
    already: set[str] = set()

    for pass_n in range(1, args.passes + 1):
        print(f"\n=== pass {pass_n}/{args.passes} ===", flush=True)
        state = args.state if pass_n == 1 else None
        ids = collect_ids(s, user, uid, state) - already
        print(f"unique new books this pass: {len(ids)}", flush=True)
        if not ids:
            print("nothing left to remove", flush=True)
            break
        if args.dry_run:
            for bid in sorted(ids)[:15]:
                print(" ", bid)
            if len(ids) > 15:
                print(f"  … and {len(ids) - 15} more")
            break

        for i, bid in enumerate(sorted(ids), 1):
            if i == 1 or i % 25 == 0:
                home = s.get(f"{BASE}/", timeout=30)
                csrf = _csrf(home.text) or csrf
            ok, code = remove_book(s, bid, csrf, remove_tags)
            already.add(bid)
            if ok:
                total_ok += 1
                print(f"[{i}/{len(ids)}] removed {bid}", flush=True)
            else:
                total_fail += 1
                print(f"[{i}/{len(ids)}] FAIL {bid} HTTP {code}", flush=True)
            time.sleep(args.sleep)

        # Ownership is separate from reading status.
        owned = collect_owned_ids(s, user)
        if owned and not args.dry_run:
            print(f"clearing owned flag on {len(owned)} books…", flush=True)
            for i, bid in enumerate(sorted(owned), 1):
                if i == 1 or i % 25 == 0:
                    home = s.get(f"{BASE}/", timeout=30)
                    csrf = _csrf(home.text) or csrf
                ok, code = remove_owned(s, bid, csrf)
                if ok:
                    total_ok += 1
                    print(f"[owned {i}/{len(owned)}] cleared {bid}", flush=True)
                else:
                    total_fail += 1
                    print(f"[owned {i}/{len(owned)}] FAIL {bid} HTTP {code}", flush=True)
                time.sleep(args.sleep)

    print(f"\nprofile after: {profile_counts(s, user)}", flush=True)
    print(f"done ok={total_ok} fail={total_fail}", flush=True)
    return 0 if total_fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
