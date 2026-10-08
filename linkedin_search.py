"""Search LinkedIn jobs and run Easy Apply on the results using Gemini for the form answers."""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from easy_apply import apply_to_current_job, dismiss_modal
from gemini_client import Gemini, GeminiError
from job_intelligence import (
    analyze_job,
    deduplicate_jobs,
    enabled_role_profiles,
    normalize_config,
    normalize_job,
    rank_jobs,
    rank_resumes,
    recency_filter,
    score_job,
)
from qa_store import QAStore
from resume_profile import (
    get_or_build_profile,
    profile_cache_path,
    validate_resume_profile,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
LINKS_PATH = Path(__file__).resolve().parent / "links.txt"
NOT_TARGETED_PATH = Path(__file__).resolve().parent / "not_targeted_jobs.txt"
NOT_TARGETED_JSON_PATH = Path(__file__).resolve().parent / "not_targeted_jobs.json"
APPLIED_JOBS_PATH = Path(__file__).resolve().parent / "applied_jobs.json"
RESUMES_DIR = Path(__file__).resolve().parent / "resumes"

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = { runtime: {} };
"""

# `/jobs/search/` renders the classic list: every card is an <li> carrying
# data-occludable-job-id, but only the ones near the viewport are hydrated,
# so each must be scrolled into view before its contents exist.
JOB_LIST_ITEM_SELECTORS = [
    "li[data-occludable-job-id]",
    "li.scaffold-layout__list-item",
    "div.job-card-container",
    "div[data-job-id]",
]

JOB_LIST_CONTAINER_SELECTORS = [
    "div.scaffold-layout__list > ul",
    "ul.scaffold-layout__list-container",
    ".jobs-search-results-list",
]

APPLICANT_COUNT_SELECTORS = [
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card__applicant-count",
    ".job-details-jobs-unified-top-card__tertiary-description-container",
]

JOB_TITLE_SELECTORS = [
    ".job-details-jobs-unified-top-card__job-title",
    ".jobs-unified-top-card__job-title",
    "h1.t-24",
    "h1",
]

JOB_COMPANY_SELECTORS = [
    ".job-details-jobs-unified-top-card__company-name",
    ".jobs-unified-top-card__company-name",
]

JOB_LOCATION_SELECTORS = [
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".jobs-unified-top-card__tertiary-description-container",
]

JOB_DESCRIPTION_SELECTORS = [
    ".jobs-description__content",
    ".jobs-box__html-content",
]

JOB_INSIGHT_SELECTORS = [
    ".job-details-jobs-unified-top-card__job-insight",
    ".jobs-unified-top-card__job-insight",
    ".job-details-jobs-unified-top-card__primary-description-container",
]


def load_config(path="config.json"):
    if not os.path.exists(path):
        print(f"{path} not found. Create it first.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return normalize_config(json.load(f))


def first_visible(scope, selectors, limit=10):
    for sel in selectors:
        locs = scope.locator(sel)
        try:
            count = locs.count()
        except Exception:
            continue
        for i in range(min(count, limit)):
            loc = locs.nth(i)
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue
    return None


def text_of(page, selectors):
    loc = first_visible(page, selectors, limit=3)
    if not loc:
        return ""
    try:
        return (loc.inner_text() or "").strip()
    except Exception:
        return ""


def find_easy_apply_button(page):
    """The details panel uses hashed class names, so match the button by role/name."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"Easy Apply", re.I)),
        page.locator("button.jobs-apply-button"),
        page.locator('button:has-text("Easy Apply")'),
    ]
    for locs in candidates:
        try:
            count = locs.count()
        except Exception:
            continue
        for i in range(min(count, 5)):
            loc = locs.nth(i)
            try:
                if loc.is_visible() and loc.is_enabled():
                    return loc
            except Exception:
                continue
    return None


def click_easy_apply(page, timeout=20000):
    start = time.time()
    while (time.time() - start) * 1000 < timeout:
        loc = find_easy_apply_button(page)
        if loc:
            loc.scroll_into_view_if_needed()
            try:
                loc.click()
            except Exception:
                loc.evaluate("el => el.click()")
            return True
        page.wait_for_timeout(500)
    return False


def search_url(keywords, config):
    """Build a classic /jobs/search/ URL.

    This matters: the global search bar routes to /jobs/search-results/, LinkedIn's
    server-driven-UI page whose cards are hashed `componentkey` divs with no job id
    and no anchor. /jobs/search/ still serves the classic, automatable DOM.
    """
    params = {"keywords": keywords, "f_AL": "true"}
    params["f_TPR"] = recency_filter(config.get("posted_within_days", 7))
    if config.get("location"):
        params["location"] = config["location"]
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


def _classic_search_url(url):
    """Make a saved LinkedIn results link safe for deterministic pagination."""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.replace("/jobs/search-results/", "/jobs/search/")
    ignored = {"currentJobId", "origin", "referralSearchId", "trk"}
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in ignored
    ]
    return urllib.parse.urlunsplit(parsed._replace(path=path, query=urllib.parse.urlencode(query)))


def open_jobs_search(page, keywords, config, target_url=None):
    url = _classic_search_url(target_url) if target_url else search_url(keywords, config)
    print(f"Opening: {url}")
    try:
        # LinkedIn often keeps network requests open indefinitely. Waiting for
        # the DOM is enough; the result list is hydrated below afterward.
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=int(config.get("navigation_timeout_ms", 60000)),
        )
    except PlaywrightTimeoutError:
        print("Navigation is still loading; continuing with the page received so far.")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(config.get("page_load_wait_ms", 3000))

    if "/jobs/search-results/" in page.url:
        print("LinkedIn opened the SDUI results page; switching the same search to the classic list.")
        classic_url = url.replace("/jobs/search-results/", "/jobs/search/")
        try:
            page.goto(
                classic_url,
                wait_until="domcontentloaded",
                timeout=int(config.get("navigation_timeout_ms", 60000)),
            )
        except PlaywrightTimeoutError:
            print("Classic results are still loading; continuing with the page received so far.")
        page.wait_for_timeout(config.get("page_load_wait_ms", 3000))

    print(f"Jobs URL: {page.url}")


def hydrate_cards(page, passes=4):
    """Scroll the results list so occluded cards render, then report the count."""
    container = first_visible(page, JOB_LIST_CONTAINER_SELECTORS, limit=2)
    last = 0
    for _ in range(passes):
        cards = job_cards(page)
        count = cards.count() if cards else 0
        if count and count == last:
            break
        last = count
        if container:
            try:
                container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
            except Exception:
                page.mouse.wheel(0, 1500)
        else:
            page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1200)

    if container:
        try:
            container.evaluate("el => el.scrollTo(0, 0)")
        except Exception:
            pass
    page.wait_for_timeout(800)
    cards = job_cards(page)
    return cards.count() if cards else 0


def job_cards(page):
    for sel in JOB_LIST_ITEM_SELECTORS:
        locs = page.locator(sel)
        try:
            if locs.count() > 0:
                return locs
        except Exception:
            continue
    return None


def applicant_count(page):
    """Parse the '36 applicants' figure from the details panel, if shown."""
    for sel in APPLICANT_COUNT_SELECTORS:
        loc = page.locator(sel)
        try:
            for i in range(min(loc.count(), 3)):
                text = (loc.nth(i).inner_text() or "")
                match = re.search(r"([\d,]+)\s+applicant", text, re.I)
                if match:
                    return int(match.group(1).replace(",", ""))
        except Exception:
            continue
    return None


def job_context(page):
    location_text = text_of(page, JOB_LOCATION_SELECTORS)
    parts = [part.strip() for part in location_text.split("·") if part.strip()]
    insights = []
    for selector in JOB_INSIGHT_SELECTORS:
        locators = page.locator(selector)
        try:
            insights.extend((locators.nth(i).inner_text() or "").strip() for i in range(min(locators.count(), 10)))
        except Exception:
            continue
    structured = " | ".join(parts + insights)
    workplace = next((name for name in ("Remote", "Hybrid", "On-site") if name.lower() in structured.lower()), None)
    employment = next((name for name in ("Full-time", "Part-time", "Contract", "Temporary", "Internship") if name.lower() in structured.lower()), None)
    seniority = next((name for name in ("Internship", "Entry level", "Associate", "Mid-Senior level", "Director", "Executive") if name.lower() in structured.lower()), None)
    posting = next((part for part in parts if re.search(r"\b(?:minute|hour|day|week|month)s? ago\b|reposted", part, re.I)), None)
    return {
        "title": text_of(page, JOB_TITLE_SELECTORS),
        "company": text_of(page, JOB_COMPANY_SELECTORS),
        "location": parts[0] if parts else location_text,
        "employment_type": employment,
        "workplace_type": workplace,
        "seniority": seniority,
        "posting_date": posting,
        "description": text_of(page, JOB_DESCRIPTION_SELECTORS),
        "url": page.url,
        "application_method": "EASY_APPLY" if find_easy_apply_button(page) else "EXTERNAL",
    }


def load_resume_profiles(config, gemini=None):
    resumes = list(RESUMES_DIR.glob("*.pdf")) + list(RESUMES_DIR.glob("*.docx")) + list(RESUMES_DIR.glob("*.doc"))
    for key in ("default_resume_path", "resume_path"):
        configured = config.get(key)
        if configured and Path(configured).is_file():
            resumes.append(Path(configured))
    resumes = list(dict.fromkeys(resumes))
    profiles = {}
    for path in resumes:
        try:
            if gemini is not None:
                profile = get_or_build_profile(path, gemini=gemini)
            else:
                cached = profile_cache_path(path)
                if not cached.exists():
                    continue
                profile = validate_resume_profile(json.loads(cached.read_text(encoding="utf-8")))
            profiles[str(path)] = profile
        except Exception as exc:
            print(f"  [warn] resume profile unavailable for {path.name}: {exc}")
    return profiles


def choose_resume_path(ctx, config, gemini=None, resume_profiles=None):
    profiles = resume_profiles if resume_profiles is not None else load_resume_profiles(config, gemini)
    if profiles:
        job = analyze_job(normalize_job(ctx, ""))
        ranked = rank_resumes(job, profiles)
        if ranked:
            best = ranked[0]
            print(f"  Recommended resume: {Path(best['path']).name} ({best['score']}/100)")
            print(f"  Resume match: {best['reasoning']}")
            return best["path"]

    configured = config.get("default_resume_path") or config.get("resume_path")
    if configured and os.path.exists(configured):
        return configured

    resumes = list(RESUMES_DIR.glob("*.pdf")) + list(RESUMES_DIR.glob("*.docx")) + list(RESUMES_DIR.glob("*.doc"))
    text = " ".join((ctx.get(k) or "") for k in ("title", "company", "description")).lower()
    groups = {
        "backend": ("backend", "back-end", "django", "fastapi", "api", "server"),
        "frontend": ("frontend", "front-end", "react", "vue", "javascript", "ui", "web"),
        "fullstack": ("fullstack", "full-stack", "full stack", "mern", "node"),
        "dataengineer": ("data engineer", "data pipeline", "etl", "airflow", "spark", "warehouse"),
        "dataanalyst": ("data analyst", "data analysis", "sql", "analytics", "bi analyst"),
        "softwareengineer": ("software engineer", "software developer", "engineering", "developer"),
        "fde": ("founding", "founder", "early stage", "full-stack", "backend", "frontend"),
    }

    def score(path):
        stem = re.sub(r"[^a-z0-9]+", " ", path.stem.lower())
        matched_group = next((group for group in groups if group in stem), "")
        keywords = groups.get(matched_group, ())
        value = sum(3 for keyword in keywords if keyword in text)
        value += sum(2 for keyword in keywords if keyword in stem)
        if matched_group and matched_group in text:
            value += 5
        return value

    ranked = sorted(resumes, key=lambda path: (-score(path), path.name.lower()))
    if ranked and score(ranked[0]) > 0:
        print("  [warn] using filename-only resume matching because no profile is available")
        return str(ranked[0])
    return None


def job_id(ctx):
    if ctx.get("job_id"):
        return str(ctx["job_id"])
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(ctx.get("url", "")).query)
    identifier = (query.get("currentJobId") or [None])[0]
    if identifier:
        return identifier
    match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:/|$)", ctx.get("url", ""))
    return match.group(1) if match else None


def load_applied_ids():
    if not APPLIED_JOBS_PATH.exists():
        return set()
    try:
        data = json.loads(APPLIED_JOBS_PATH.read_text(encoding="utf-8"))
        return {
            str(value.get("job_id")) if isinstance(value, dict) else str(value)
            for value in data
            if (value.get("job_id") if isinstance(value, dict) else value)
        }
    except (OSError, json.JSONDecodeError):
        return set()


def save_applied_id(identifier, ctx=None, role=None):
    records = {}
    if APPLIED_JOBS_PATH.exists():
        try:
            data = json.loads(APPLIED_JOBS_PATH.read_text(encoding="utf-8"))
            for value in data if isinstance(data, list) else []:
                item_id = value.get("job_id") if isinstance(value, dict) else value
                if item_id:
                    records[str(item_id)] = (
                        {**value, "job_id": str(item_id), "status": "applied"}
                        if isinstance(value, dict)
                        else {"job_id": str(item_id), "status": "applied"}
                    )
        except (OSError, json.JSONDecodeError):
            pass
    record = {"job_id": str(identifier), "status": "applied"}
    if ctx:
        record.update(
            {
                "title": (ctx.get("title") or "").strip(),
                "company": (ctx.get("company") or "").strip(),
                "location": (ctx.get("location") or "").strip(),
                "url": ctx.get("url") or "",
                "target_role": role or "",
            }
        )
    records[str(identifier)] = record
    APPLIED_JOBS_PATH.write_text(
        json.dumps(sorted(records.values(), key=lambda item: item["job_id"]), indent=2),
        encoding="utf-8",
    )


def not_targeted_key(ctx):
    identifier = job_id(ctx)
    if identifier:
        return f"linkedin:{identifier}"
    url = (ctx.get("url") or "").split("&", 1)[0]
    if url:
        return f"url:{url}"
    parts = [re.sub(r"\s+", " ", (ctx.get(key) or "").strip().lower())
             for key in ("title", "company", "location")]
    return "details:" + "|".join(parts)


def load_not_targeted():
    if not NOT_TARGETED_JSON_PATH.exists():
        return {}
    try:
        data = json.loads(NOT_TARGETED_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        item.get("key"): item
        for item in data
        if isinstance(item, dict) and item.get("key")
    }


def save_not_targeted(ctx, reason, role):
    title = (ctx.get("title") or "Untitled job").replace("\n", " ").strip()
    company = (ctx.get("company") or "Unknown company").replace("\n", " ").strip()
    location = (ctx.get("location") or "Unknown location").replace("\n", " ").strip()
    key = not_targeted_key(ctx)
    records = load_not_targeted()
    records[key] = {
        "key": key,
        "status": "not_targeted",
        "job_id": job_id(ctx),
        "title": title,
        "company": company,
        "location": location,
        "url": ctx.get("url") or "",
        "reason": reason,
        "target_role": role,
    }
    NOT_TARGETED_JSON_PATH.write_text(
        json.dumps(sorted(records.values(), key=lambda item: item["key"]), indent=2),
        encoding="utf-8",
    )
    
    line = f"{title} | {company} | {location} | {reason} | target: {role}"
    existing = (NOT_TARGETED_PATH.read_text(encoding="utf-8").splitlines()
                if NOT_TARGETED_PATH.exists() else [])
    lines = list(dict.fromkeys(existing + [line]))
    NOT_TARGETED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def allowed_location(ctx):
    # Use the structured header fields only. Searching the full description
    # would incorrectly classify jobs that merely mention remote work.
    searchable = " ".join(
        (ctx.get(key) or "") for key in ("title", "company", "location")
    ).lower()
    remote = "remote" in searchable or "work from home" in searchable
    karachi = "karachi" in searchable
    onsite_or_hybrid = any(
        word in searchable for word in ("on-site", "onsite", "on site", "hybrid")
    )
    return remote or (karachi and onsite_or_hybrid)


def load_links(path=LINKS_PATH):
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def already_applied(page):
    for sel in [
        '.jobs-s-apply span:has-text("Applied")',
        '.artdeco-inline-feedback:has-text("Applied")',
        'span:has-text("Application submitted")',
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _collect_current_search(page, role_profile, config):
    keywords = " OR ".join(role_profile.get("keywords") or [role_profile["name"]])
    open_jobs_search(page, keywords, config, target_url=role_profile.get("target_url"))
    total = hydrate_cards(page)
    print(f"{total} job card(s) for {role_profile['name']}.")
    jobs = []
    for index in range(total):
        cards = job_cards(page)
        if not cards or index >= cards.count():
            break
        card = cards.nth(index)
        try:
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            card.click()
            page.wait_for_timeout(1500)
            raw = job_context(page)
            raw["applicant_count"] = applicant_count(page)
            jobs.append(normalize_job(raw, role_profile["name"]))
        except Exception as exc:
            print(f"[{index + 1}] could not collect card: {exc}")
    return jobs


def _page_url(url, page_number):
    if not page_number:
        return url
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["start"] = str(page_number * 25)
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def discover_jobs(page, config):
    links = load_links()
    if config.get("target_url"):
        links = [config["target_url"]]
    if links:
        sources = [
            {
                "name": "roles from links.txt",
                "priority": 1,
                "keywords": [],
                "minimum_fit_score": config.get("minimum_fit_score", 70),
                "enabled": True,
                "target_url": link,
            }
            for link in links
        ]
    else:
        sources = enabled_role_profiles(config)

    discovered = []
    for profile in sources:
        if links:
            base_url = profile["target_url"]
        else:
            keywords = " OR ".join(profile.get("keywords") or [profile["name"]])
            base_url = search_url(keywords, config)
        for page_number in range(int(config.get("max_search_pages", 10))):
            page_profile = {**profile, "target_url": _page_url(base_url, page_number)}
            batch = _collect_current_search(page, page_profile, config)
            discovered.extend(batch)
            if not batch:
                break
    return deduplicate_jobs(discovered)


def prepare_ranked_jobs(jobs, resume_profiles, config, gemini=None):
    profiles_by_name = {profile["name"]: profile for profile in enabled_role_profiles(config)}
    prepared = []
    for job in jobs:
        analyzed = analyze_job(job, gemini)
        resume_ranking = rank_resumes(analyzed, resume_profiles)
        selected = choose_resume_path(
            analyzed,
            config,
            gemini=gemini,
            resume_profiles=resume_profiles,
        )
        if not selected or selected not in resume_profiles:
            print(f"  [warn] no validated resume for {analyzed.get('title') or 'untitled job'}")
            continue
        matched_profiles = [
            profiles_by_name[name]
            for name in analyzed.get("matched_roles", [])
            if name in profiles_by_name
        ] or list(profiles_by_name.values())
        fit = score_job(analyzed, resume_profiles[selected], matched_profiles, config)
        prepared.append(
            {
                "job": analyzed,
                "selected_resume": selected,
                "resume_profile": resume_profiles[selected],
                "resume_ranking": resume_ranking,
                "fit": fit,
                "role_priority": min(
                    (profile.get("priority", 3) for profile in matched_profiles),
                    default=3,
                ),
            }
        )
    return rank_jobs(prepared, config)


def apply_ranked_jobs(page, ranked_jobs, config, gemini, store, applied_ids):
    max_applicants = int(config.get("applicants") or 0) or None
    max_applications = int(config.get("max_applications", 5))
    applied = skipped = 0
    for index, item in enumerate(ranked_jobs):
        if applied >= max_applications:
            break
        job = item["job"]
        fit = item["fit"]
        role_label = ", ".join(job.get("matched_roles") or ["unknown role"])
        identifier = job.get("job_id")
        if fit["tier"] not in ("A", "B"):
            save_not_targeted(job, f"fit score {fit['score']} below threshold", role_label)
            skipped += 1
            continue
        if identifier and identifier in applied_ids:
            skipped += 1
            continue
        if load_not_targeted().get(not_targeted_key(job)):
            skipped += 1
            continue
        count = job.get("applicant_count")
        if max_applicants and count is not None and count > max_applicants:
            save_not_targeted(job, f"over {max_applicants}-applicant cap", role_label)
            skipped += 1
            continue
        if job.get("application_method") == "EXTERNAL":
            save_not_targeted(job, "external application required", role_label)
            print(f"EXTERNAL APPLICATION REQUIRED: {job.get('url')}")
            skipped += 1
            continue

        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=int(config.get("navigation_timeout_ms", 60000)))
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(config.get("page_load_wait_ms", 3000))
        live = job_context(page)
        live_count = applicant_count(page)
        live_id = job_id(live)
        if identifier and live_id and live_id != identifier:
            save_not_targeted(job, "job identity changed after navigation", role_label)
            skipped += 1
            continue
        job = {
            **job,
            **{key: value for key, value in live.items() if value not in (None, "")},
        }
        if live_count is not None:
            job["applicant_count"] = live_count
        count = job.get("applicant_count")
        if max_applicants and count is not None and count > max_applicants:
            save_not_targeted(job, f"over {max_applicants}-applicant cap", role_label)
            skipped += 1
            continue
        if already_applied(page):
            skipped += 1
            continue
        if not click_easy_apply(page, timeout=8000):
            save_not_targeted(job, "no Easy Apply button", role_label)
            skipped += 1
            continue
        try:
            sent = apply_to_current_job(
                page,
                gemini,
                item["resume_profile"],
                job,
                resume_path=item["selected_resume"],
                store=store,
            )
        except GeminiError:
            raise
        except Exception as exc:
            print(f"  [error] {exc}")
            page.screenshot(path=f"error_apply_{index + 1}.png")
            sent = False
        if sent:
            applied += 1
            if identifier:
                save_applied_id(identifier, job, role_label)
                applied_ids.add(identifier)
        else:
            save_not_targeted(job, "application not submitted", role_label)
            skipped += 1
            dismiss_modal(page)
    return applied, skipped


def run(config):
    gemini = Gemini(config)
    store = QAStore()
    print(f"Loaded {len(store)} cached question/answer pair(s).")
    if not os.path.exists("linkedin_state.json"):
        print("linkedin_state.json not found. Run linkedin_login.py first.")
        sys.exit(1)

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": bool(config.get("headless", False)),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        try:
            browser = p.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            storage_state="linkedin_state.json",
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()
        resume_profiles = load_resume_profiles(config, gemini)
        if not resume_profiles:
            context.close()
            browser.close()
            raise RuntimeError("No resume profile could be loaded or built")

        jobs = discover_jobs(page, config)
        ranked = prepare_ranked_jobs(jobs, resume_profiles, config, gemini)
        print(f"\nRanked {len(ranked)} unique job(s) from {len(jobs)} discovery record(s).")
        for item in ranked:
            job, fit = item["job"], item["fit"]
            print(f"  [{fit['tier']}] {fit['score']}/100 {job['title']} @ {job['company']} -> {Path(item['selected_resume']).name}")

        applied, skipped = apply_ranked_jobs(
            page, ranked, config, gemini, store, load_applied_ids()
        )
        print(f"\nDone. Applied to {applied} job(s), skipped {skipped}.")
        context.close()
        browser.close()
        return len(jobs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", nargs="?", help="Override config.json role")
    parser.add_argument("applicants", nargs="?", help="Override config.json applicants")
    parser.add_argument("--max", type=int, help="Override max_applications")
    args = parser.parse_args()

    config = load_config()
    if args.role:
        config["role"] = args.role
        config["target_roles"] = [
            {
                "name": args.role,
                "priority": 1,
                "keywords": [args.role],
                "minimum_fit_score": config.get("minimum_fit_score", 70),
                "enabled": True,
            }
        ]
    if args.applicants:
        config["applicants"] = args.applicants
    if args.max:
        config["max_applications"] = args.max

    run(normalize_config(config))


if __name__ == "__main__":
    main()
