#!/usr/bin/env python3
"""Resolve job-listing URLs from a parsed job list using SearXNG.

This intentionally produces *candidates with evidence*, not unverified claims
that a URL is the original posting. It is resumable and safe to interrupt.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


STOP = {
    "the", "a", "an", "and", "or", "of", "for", "at", "in", "on", "to",
    "with", "from", "by", "inc", "llc", "ltd", "company", "co", "corp",
    "intern", "internship", "summer", "fall", "spring", "job", "jobs",
    "career", "careers", "remote", "full", "time", "part", "multiple",
}
BAD_HOSTS = {
    "wikipedia.org", "careerexplorer.com", "talent.com", "zippia.com",
    "merriam-webster.com", "tealhq.com", "linkedin.com", "glassdoor.com",
}
BOARD_HOSTS = {
    "indeed.com", "simplyhired.com", "ziprecruiter.com", "careerbuilder.com",
    "lever.co", "greenhouse.io", "ashbyhq.com", "myworkdayjobs.com",
    "workday.com", "jobvite.com", "icims.com", "smartrecruiters.com",
}


def tokens(value: str) -> set[str]:
    return {
        x for x in re.findall(r"[a-z0-9]+", value.lower())
        if len(x) > 2 and x not in STOP
    }


def host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def atomic_json(path: Path, value: object) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def score(job: dict, item: dict) -> tuple[float, str]:
    url = str(item.get("url") or "").strip()
    text = " ".join(str(item.get(k) or "") for k in ("title", "content", "snippet"))
    jt = tokens(job.get("job_title", ""))
    et = tokens(job.get("employer", ""))
    evidence = tokens(text + " " + url)
    title_hits = len(jt & evidence) / max(1, len(jt))
    employer_hits = len(et & evidence) / max(1, len(et))
    h = host(url)
    base = 0.55 * title_hits + 0.35 * employer_hits
    if any(h == bad or h.endswith("." + bad) for bad in BAD_HOSTS):
        base -= 0.70
    elif any(h == board or h.endswith("." + board) for board in BOARD_HOSTS):
        base += 0.08
    else:
        base += 0.15
    if any(x in url.lower() for x in ("/job/", "/jobs/", "/careers/", "/career/", "job.html", "requisition")):
        base += 0.10
    status = "candidate"
    if base >= 0.78 and employer_hits >= 0.5 and title_hits >= 0.45:
        status = "strong_candidate"
    if base < 0.35 or any(h == bad or h.endswith("." + bad) for bad in BAD_HOSTS):
        status = "rejected_generic_or_weak"
    return round(max(0.0, min(1.0, base)), 3), status


def search_one(job: dict, endpoint: str, timeout: int, max_results: int) -> dict:
    query = '"%s" "%s"' % (job.get("job_title", ""), job.get("employer", ""))
    try:
        request_url = endpoint + ("&" if "?" in endpoint else "?") + urlencode({
            "q": query, "format": "json", "language": "en-US", "safesearch": 0,
        })
        request = Request(request_url, headers={"User-Agent": "DelilahJobResolver/2.0"})
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        candidates = []
        for item in (payload.get("results") or [])[:max_results]:
            if not item.get("url", "").startswith(("http://", "https://")):
                continue
            confidence, status = score(job, item)
            candidates.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", item.get("snippet", "")),
                "confidence": confidence,
                "status": status,
                "domain": host(item.get("url", "")),
            })
        candidates.sort(key=lambda x: x["confidence"], reverse=True)
        selected = next((x for x in candidates if x["status"] != "rejected_generic_or_weak"), None)
        return {
            "job": job,
            "query": query,
            "selected_url": selected["url"] if selected else "",
            "selected_status": selected["status"] if selected else "no_verified_candidate",
            "selected_confidence": selected["confidence"] if selected else 0.0,
            "candidates": candidates,
            "error": "",
        }
    except Exception as exc:
        return {"job": job, "query": query, "selected_url": "", "selected_status": "error", "selected_confidence": 0.0, "candidates": [], "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=os.environ.get("FINANCEBOT_WORKSPACE", "."))
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--checkpoint-every", type=int, default=25)
    ap.add_argument("--max-results", type=int, default=8)
    args = ap.parse_args()
    ws = Path(args.workspace)
    endpoint = os.environ.get("SEARXNG_URL", "http://searxng:8080/search")
    jobs = json.loads((ws / "jobs_list.json").read_text(encoding="utf-8"))
    checkpoint_path = ws / "job_url_candidates_v2.json"
    results = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
    pending = [(i, j) for i, j in enumerate(jobs) if str(i) not in results]
    print(f"jobs={len(jobs)} existing={len(results)} pending={len(pending)} endpoint={endpoint}", flush=True)
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(search_one, job, endpoint, args.timeout, args.max_results): i for i, job in pending}
        for future in as_completed(futures):
            i = futures[future]
            results[str(i)] = future.result()
            completed += 1
            if completed % args.checkpoint_every == 0:
                atomic_json(checkpoint_path, results)
                print(f"progress={len(results)}/{len(jobs)}", flush=True)
    atomic_json(checkpoint_path, results)
    rows = []
    for i, job in enumerate(jobs):
        r = results.get(str(i), {})
        rows.append({
            "job_listing_name": job.get("full_title", ""),
            "job_title": job.get("job_title", ""),
            "employer": job.get("employer", ""),
            "category": job.get("category", ""),
            "selected_url": r.get("selected_url", ""),
            "status": r.get("selected_status", "missing"),
            "confidence": r.get("selected_confidence", 0.0),
            "query": r.get("query", ""),
            "candidate_count": len(r.get("candidates", [])),
            "error": r.get("error", ""),
        })
    with (ws / "jobs_with_url_candidates_v2.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"complete={len(results)} output=jobs_with_url_candidates_v2.csv", flush=True)


if __name__ == "__main__":
    main()
