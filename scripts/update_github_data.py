#!/usr/bin/env python3
"""Fetch GitHub profile metrics and inject into README.md markers."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USERNAME = os.environ.get("GITHUB_USERNAME", "lm041520")
TOKEN = os.environ.get("PROFILE_STATS_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
README = Path("README.md")
GRAPHQL = "https://api.github.com/graphql"
REST_USER = f"https://api.github.com/users/{USERNAME}"


def request_json(url: str, *, data: dict | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-readme",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = None if data is None else json.dumps(data).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def graphql(query: str, variables: dict | None = None) -> dict:
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    data = request_json(GRAPHQL, data=payload)
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def format_size(kb: float) -> str:
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def build_block(
    *,
    disk_kb: int,
    contributions: int,
    year: int,
    hireable: bool,
    public_repos: int,
    private_repos: int | None,
) -> str:
    hire_line = "💼 **开放求职联系**" if hireable else "🚫 **暂未开放招聘联系**"
    lines = [
        f"> 📦 GitHub 存储占用：**{format_size(disk_kb)}**",
        f"> 🏆 {year} 年贡献次数：**{contributions}**",
        f"> {hire_line}",
        f"> 📜 公开仓库：**{public_repos}**",
    ]
    if private_repos is not None:
        lines.append(f"> 🔑 私有仓库：**{private_repos}**")
    return "\n".join(lines) + "\n"


def page_owned_repos(*, use_viewer: bool) -> tuple[int, int, int]:
    """Return disk_kb, public_count, private_count."""
    disk = public_count = private_count = 0
    cursor = None

    while True:
        after = f', after: "{cursor}"' if cursor else ""
        if use_viewer:
            query = f"""
            query {{
              viewer {{
                repositories(ownerAffiliations: OWNER, first: 100{after}) {{
                  nodes {{ isPrivate diskUsage }}
                  pageInfo {{ hasNextPage endCursor }}
                }}
              }}
            }}
            """
            repos = graphql(query)["viewer"]["repositories"]
        else:
            query = f"""
            query {{
              user(login: "{USERNAME}") {{
                repositories(ownerAffiliations: OWNER, first: 100{after}) {{
                  nodes {{ isPrivate diskUsage }}
                  pageInfo {{ hasNextPage endCursor }}
                }}
              }}
            }}
            """
            repos = graphql(query)["user"]["repositories"]

        for node in repos.get("nodes") or []:
            disk += int(node.get("diskUsage") or 0)
            if node.get("isPrivate"):
                private_count += 1
            else:
                public_count += 1

        page = repos.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")

    return disk, public_count, private_count


def rest_public_repo_stats() -> tuple[int, int]:
    """Fallback via REST: (disk_kb, public_count). size field is already KB."""
    disk = count = 0
    page = 1
    while True:
        rows = request_json(
            f"https://api.github.com/users/{USERNAME}/repos?per_page=100&page={page}&type=owner&sort=updated"
        )
        if not isinstance(rows, list) or not rows:
            break
        for repo in rows:
            count += 1
            disk += int(repo.get("size") or 0)
        if len(rows) < 100:
            break
        page += 1
    return disk, count


def year_contributions() -> int:
    data = graphql(
        """
        query($login: String!) {
          user(login: $login) {
            contributionsCollection {
              contributionCalendar { totalContributions }
            }
          }
        }
        """,
        {"login": USERNAME},
    )
    return int(
        data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
    )


def main() -> None:
    year = datetime.now(timezone.utc).year

    use_viewer = False
    hireable = False
    if TOKEN:
        try:
            me = graphql("query { viewer { login isHireable } }")["viewer"]
            use_viewer = me.get("login") == USERNAME
            hireable = bool(me.get("isHireable"))
        except Exception as exc:
            print(f"viewer lookup skipped: {exc}")

    disk_kb = public_repos = private_repos = 0
    try:
        disk_kb, public_repos, private_repos = page_owned_repos(use_viewer=use_viewer)
    except Exception as exc:
        print(f"GraphQL repo paging failed, REST fallback: {exc}")
        disk_kb, public_repos = rest_public_repo_stats()
        private_repos = 0
        use_viewer = False

    try:
        contributions = year_contributions()
    except Exception as exc:
        print(f"contributions lookup failed: {exc}")
        contributions = 0

    try:
        rest = request_json(REST_USER)
        public_repos = max(public_repos, int(rest.get("public_repos") or 0))
        if rest.get("hireable") is not None and not use_viewer:
            hireable = bool(rest.get("hireable"))
    except urllib.error.HTTPError as exc:
        print(f"REST user fallback skipped: {exc.code}")

    block = build_block(
        disk_kb=disk_kb,
        contributions=contributions,
        year=year,
        hireable=hireable,
        public_repos=public_repos,
        private_repos=private_repos if use_viewer else None,
    )

    text = README.read_text(encoding="utf-8")
    updated = re.sub(
        r"<!--GITHUB_DATA_START-->.*?<!--GITHUB_DATA_END-->",
        f"<!--GITHUB_DATA_START-->\n{block}<!--GITHUB_DATA_END-->",
        text,
        count=1,
        flags=re.S,
    )
    if updated == text:
        raise SystemExit("README markers <!--GITHUB_DATA_START/END--> not found")
    README.write_text(updated, encoding="utf-8")
    try:
        print(block)
    except UnicodeEncodeError:
        print(block.encode("utf-8", errors="replace").decode("ascii", errors="replace"))


if __name__ == "__main__":
    main()
