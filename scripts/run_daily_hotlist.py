#!/usr/bin/env python3
"""Collect global trend-pulse hot lists, archive them, and push to GitHub."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO = Path.home() / "sqqy"
NEWS_DIR = REPO / "news"
SOURCES = "hackernews,google_news,reddit,github,arxiv,producthunt,google_trends"
COUNT = "15"


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)


def trend_pulse_json() -> tuple[dict[str, Any] | None, str]:
    cmd = ["uvx", "trend-pulse", "trending", "--sources", SOURCES, "--count", COUNT]
    errors = []
    for attempt in range(2):
        proc = run(cmd, timeout=420)
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout), ""
            except json.JSONDecodeError as exc:
                errors.append(f"attempt {attempt + 1}: JSON解析失败: {exc}")
        else:
            errors.append(f"attempt {attempt + 1}: exit={proc.returncode}; {proc.stderr.strip() or proc.stdout.strip()}")
    return None, " | ".join(errors)


def fetch_text(url: str, limit: int = 5000) -> str:
    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 hermes-hotlist-archiver"})
        with urlopen(req, timeout=8) as resp:
            data = resp.read(limit)
            ctype = resp.headers.get("content-type", "")
        if "text" not in ctype and "html" not in ctype and "json" not in ctype:
            return ""
        text = data.decode("utf-8", errors="ignore")
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    except (URLError, TimeoutError, OSError, ValueError):
        return ""


def summarize_items(items: list[dict[str, Any]]) -> dict[int, str]:
    payload = []
    for idx, item in enumerate(items, start=1):
        title = str(item.get("keyword") or "").strip()
        url = str(item.get("url") or "").strip()
        text = fetch_text(url)
        payload.append({"id": idx, "title": title, "text": text[:1200], "readable": bool(text)})

    prompt = (
        "你是每日热榜归档助手。只根据给定 title 和 text 写中文摘要，不能补充材料外事实。"
        "每条摘要不超过30个中文字符；text为空时必须在摘要末尾加(仅据标题)。"
        "不确定就写(标题已自解释)。只输出JSON对象，键为id字符串，值为摘要。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    proc = run(["hermes", "chat", "--quiet", "--yolo", "-q", prompt], cwd=REPO, timeout=480)
    if proc.returncode != 0:
        return {idx: fallback_summary(item) for idx, item in enumerate(items, start=1)}
    text = proc.stdout.strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {idx: fallback_summary(item) for idx, item in enumerate(items, start=1)}
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {idx: fallback_summary(item) for idx, item in enumerate(items, start=1)}
    result: dict[int, str] = {}
    for idx, item in enumerate(items, start=1):
        value = str(obj.get(str(idx), "")).strip()
        result[idx] = value[:60] if value else fallback_summary(item)
    return result


def fallback_summary(item: dict[str, Any]) -> str:
    title = str(item.get("keyword") or "").strip()
    if not title:
        return ""
    return "(标题已自解释)" if len(title) <= 30 else f"{title[:30]}(仅据标题)"


def render(data: dict[str, Any] | None, error: str = "") -> str:
    today = datetime.now().strftime("%F")
    lines = [
        f"# 每日热榜 {today}",
        f"源:{SOURCES}",
        "纯热榜按热度排序,原样归档,不分频道不预筛",
        "",
    ]
    if data is None:
        lines.append(f"{today} 抓取失败:{error}")
        return "\n".join(lines) + "\n"

    src_err = data.get("sources_error") or {}
    if src_err:
        lines.append(f"源错误:{json.dumps(src_err, ensure_ascii=False)}")
        lines.append("")

    merged = data.get("merged") or []
    merged = sorted(merged, key=lambda x: float(x.get("score") or 0), reverse=True)
    if not merged:
        lines.append("今日无热榜条目。")
        return "\n".join(lines) + "\n"

    summaries = summarize_items(merged)
    for idx, item in enumerate(merged, start=1):
        score = item.get("score", "")
        source = item.get("source", "")
        title = str(item.get("keyword") or "").strip()
        traffic = str(item.get("traffic") or "").strip()
        url = str(item.get("url") or "").strip()
        lines.append(f"{idx}. [{score}|{source}] {title}")
        lines.append(f"   摘要：{summaries.get(idx, fallback_summary(item))}")
        lines.append(f"   {traffic} | {url}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def git_push(path: Path) -> str:
    run(["git", "add", str(path.relative_to(REPO))], cwd=REPO)
    diff = run(["git", "diff", "--cached", "--quiet"], cwd=REPO)
    if diff.returncode == 0:
        return "no changes"
    msg = f"热榜 {datetime.now().strftime('%F')}"
    commit = run(["git", "commit", "-m", msg], cwd=REPO)
    if commit.returncode != 0:
        return f"commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
    last_push = None
    for _ in range(2):
        last_push = run(["git", "push"], cwd=REPO, timeout=180)
        if last_push.returncode == 0:
            return "pushed"
    if last_push is None:
        return "push failed: not attempted"
    return f"push failed: {last_push.stderr.strip() or last_push.stdout.strip()}"


def main() -> int:
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    data, error = trend_pulse_json()
    out = NEWS_DIR / f"{datetime.now().strftime('%F')}.md"
    out.write_text(render(data, error), encoding="utf-8")
    status = git_push(out)
    print(f"wrote {out}")
    print(status)
    if error:
        print(error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
