#!/usr/bin/env python3
"""Update the profile README with two generated blocks.

SIZE  -> top languages across your public repos, in `arm-none-eabi-size` format
DMESG -> your latest public commits / PRs / issues, in `dmesg` format

Column meaning for the SIZE block (decorative but computed from real data):
  text = bytes of that language in repos NOT pushed in the last ACTIVE_DAYS
  data = bytes of that language in repos pushed in the last ACTIVE_DAYS
  bss  = number of repos that use the language
  dec  = text + data + bss
  hex  = dec in hexadecimal

Only the standard library is used (no pip install needed).

Usage:
  GITHUB_TOKEN=$(gh auth token) python scripts/update_profile.py --dry-run
  GITHUB_TOKEN=... python scripts/update_profile.py
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config ----
USER = os.environ.get("GH_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "jackestar"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
README = "README.md"

TOP_LANGS = 5
ACTIVE_DAYS = 90
DMESG_LINES = 8
MSG_MAX = 60  # max chars of a commit message / title in the dmesg block
EXCLUDE_LANGS = {
    x.strip() for x in os.environ.get("EXCLUDE_LANGS", "").split(",") if x.strip()
}  # e.g. EXCLUDE_LANGS="HTML,CSS,Roff"
BOT_PREFIX = "chore(profile)"  # commits made by this action are hidden from dmesg

API = "https://api.github.com"


# ------------------------------------------------------------------- api ----
def api(path):
    url = path if path.startswith("http") else API + path
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{USER}-profile-readme",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clean(text, limit=MSG_MAX):
    text = (text or "").strip().splitlines()[0] if (text or "").strip() else "(no message)"
    text = text.replace("```", "'''").replace("`", "'")
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ------------------------------------------------------------------ size ----
def owned_repos():
    repos, page = [], 1
    while True:
        batch = api(f"/users/{USER}/repos?type=owner&per_page=100&page={page}")
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    return [r for r in repos if not r["fork"] and not r["archived"]]


def size_block():
    cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVE_DAYS)
    cold = defaultdict(int)
    hot = defaultdict(int)
    count = defaultdict(int)

    for repo in owned_repos():
        try:
            langs = api(f"/repos/{repo['full_name']}/languages")
        except urllib.error.HTTPError:
            continue
        active = parse_time(repo["pushed_at"]) >= cutoff if repo.get("pushed_at") else False
        for lang, nbytes in langs.items():
            if lang in EXCLUDE_LANGS:
                continue
            (hot if active else cold)[lang] += nbytes
            count[lang] += 1

    langs = set(cold) | set(hot)
    top = sorted(langs, key=lambda l: cold[l] + hot[l], reverse=True)[:TOP_LANGS]

    rows = []
    for lang in top:
        text, data, bss = cold[lang], hot[lang], count[lang]
        dec = text + data + bss
        rows.append((str(text), str(data), str(bss), str(dec), format(dec, "x"), lang))

    header = ("text", "data", "bss", "dec", "hex", "filename")
    width = max([7] + [len(c) for row in rows for c in row[:5]])
    out = [f"$ arm-none-eabi-size {USER}/*"]
    out.append(" ".join(h.rjust(width) for h in header[:5]) + " " + header[5])
    for row in rows:
        out.append(" ".join(c.rjust(width) for c in row[:5]) + " " + row[5])
    return "\n".join(out)


# ----------------------------------------------------------------- dmesg ----
def short_repo(full_name):
    prefix = USER + "/"
    return full_name[len(prefix):] if full_name.startswith(prefix) else full_name


def push_info(event):
    payload = event["payload"]
    commits = payload.get("commits") or []
    if commits:
        last = commits[-1]
        message, sha = last["message"], last["sha"]
        count = payload.get("size", len(commits))
    else:
        # Fallback when the events payload carries no commit list.
        sha = payload.get("head")
        if not sha:
            return None
        try:
            message = api(f"/repos/{event['repo']['name']}/commits/{sha}")["commit"]["message"]
        except Exception:
            message = ""
        count = payload.get("size") or 1
    if message.startswith(BOT_PREFIX):
        return None
    return clean(message), sha[:7], count


def describe(event):
    kind = event["type"]
    repo = short_repo(event["repo"]["name"])
    payload = event["payload"]

    if kind == "PushEvent":
        info = push_info(event)
        if not info:
            return None
        message, sha, count = info
        extra = f" +{count - 1}" if count > 1 else ""
        return "commit", f"{repo}: {message} ({sha}{extra})"

    if kind == "PullRequestEvent":
        pr = payload["pull_request"]
        action = payload["action"]
        if action == "closed" and pr.get("merged"):
            action = "merged"
        if action not in {"opened", "closed", "merged", "reopened"}:
            return None
        return "pr", f"{repo}#{pr['number']} {action}: {clean(pr['title'])}"

    if kind == "IssuesEvent":
        issue = payload["issue"]
        action = payload["action"]
        if action not in {"opened", "closed", "reopened"}:
            return None
        return "issue", f"{repo}#{issue['number']} {action}: {clean(issue['title'])}"

    return None  # extend here: IssueCommentEvent, CreateEvent, ReleaseEvent...


def dmesg_block():
    events = api(f"/users/{USER}/events/public?per_page=100")
    entries = []
    for event in events:
        item = describe(event)
        if item:
            entries.append((parse_time(event["created_at"]), *item))

    out = [f"$ dmesg | tail -n {DMESG_LINES}"]
    if not entries:
        out.append("[    0.000000] " + f"{USER}: idle")
        return "\n".join(out)

    # 1. Sort strictly chronologically (oldest to newest)
    entries.sort(key=lambda x: x[0])

    # 2. Keep only the most recent DMESG_LINES entries
    entries = entries[-DMESG_LINES:]

    # 3. entries[0] is now guaranteed to be the minimum timestamp
    t0 = entries[0][0]
    stamps = [f"{(when - t0).total_seconds():.6f}" for when, _, _ in entries]
    width = max(12, max(len(s) for s in stamps))
    for stamp, (_, tag, text) in zip(stamps, entries):
        out.append(f"[{stamp.rjust(width)}] {tag:<7} {text}")
    return "\n".join(out)


# ---------------------------------------------------------------- readme ----
def replace_block(content, name, body):
    pattern = re.compile(rf"(<!--{name}:START-->)(.*?)(<!--{name}:END-->)", re.S)
    if not pattern.search(content):
        raise SystemExit(f"README is missing the <!--{name}:START--> / <!--{name}:END--> markers")
    return pattern.sub(lambda m: f"{m.group(1)}\n```text\n{body}\n```\n{m.group(3)}", content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the blocks, don't touch README")
    args = parser.parse_args()

    blocks = {}
    for name, builder in (("SIZE", size_block), ("DMESG", dmesg_block)):
        try:
            blocks[name] = builder()
        except Exception as exc:  # keep the previous block if the API hiccups
            print(f"warning: could not build {name}: {exc}", file=sys.stderr)

    if args.dry_run:
        for body in blocks.values():
            print(body, end="\n\n")
        return

    with open(README, encoding="utf-8") as fh:
        original = fh.read()
    updated = original
    for name, body in blocks.items():
        updated = replace_block(updated, name, body)
    if updated != original:
        with open(README, "w", encoding="utf-8") as fh:
            fh.write(updated)
        print("README.md updated")
    else:
        print("no changes")


if __name__ == "__main__":
    main()