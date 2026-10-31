import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen_jobs.json"
MAX_JOBS = 8
MAX_AGE_DAYS = 30
ARBEITNOW_PAGES = 3
REMOTIVE_SEARCHES = ("QA", "tester")
HEADERS = {"User-Agent": "DailyQAJobsBot/1.0"}

STAFFAM_URL = "https://staff.am/en/jobs/quality-assurance"
# staff.am is a normal website, so it gets a browser-like User-Agent.
STAFFAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Word-boundary regex so "qa" doesn't match inside other words, and
# more QA job-title variants are recognised.
QA_PATTERN = re.compile(
    r"\bqa\b|\bqc\b|\bsdet\b|quality assurance|quality engineer|"
    r"quality analyst|\btesters?\b|software testing|test engineer|"
    r"test automation|test analyst",
    re.IGNORECASE,
)

REMOTIVE_LOCATIONS = ("worldwide", "anywhere", "europe", "emea", "armenia")


def is_qa(job):
    return bool(QA_PATTERN.search(job.get("title") or ""))


def parse_date(value):
    """Accepts ISO strings OR unix timestamps (Arbeitnow uses a number)."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        text = str(value).strip()
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        posted = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def is_recent(posted):
    if posted is None:  # unknown date -> don't throw the job away
        return True
    return posted >= datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)


def get_json(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=25)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------- Staff.am
JOB_HREF = re.compile(r"/jobs/quality-assurance/[^/?#]+/?$")
DATE_IN_TEXT = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")


def parse_deadline(text):
    """Finds a date like '15 November 2026' in the job card text."""
    for day, month_name, year in DATE_IN_TEXT.findall(text):
        month = MONTHS.get(month_name.lower())
        if month:
            try:
                return datetime(int(year), month, int(day), 23, 59,
                                tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def fetch_staffam(stats):
    response = requests.get(STAFFAM_URL, headers=STAFFAM_HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    found = []
    done = set()
    now = datetime.now(timezone.utc)

    for link in soup.find_all("a", href=True):
        href = link["href"].split("?")[0].split("#")[0]
        if not JOB_HREF.search(href):
            continue
        title = link.get_text(" ", strip=True)
        if not title or title.lower() == "view more":
            continue
        url = urljoin("https://staff.am", href)
        if url in done:
            continue
        done.add(url)
        stats["Staff.am"]["fetched"] += 1

        # Walk up to the job "card" that also contains the company link.
        card = link
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if card.find("a", href=re.compile(r"/company/")):
                break

        company = "Not specified"
        for company_link in card.find_all("a", href=re.compile(r"/company/")):
            name = company_link.get_text(" ", strip=True)
            if name:
                company = name
                break

        card_text = card.get_text(" ", strip=True)
        deadline = parse_deadline(card_text)
        if deadline and deadline < now:
            continue  # expired

        location = "Yerevan" if "yerevan" in card_text.lower() else "Armenia"
        if "remote" in card_text.lower():
            location += " (Remote)"

        if is_qa({"title": title}):
            found.append({
                "title": title,
                "company": company,
                "location": location,
                "salary": "Not specified",
                "url": url,
                "date": "",  # the listing shows a deadline, not a post date
                "deadline": deadline.strftime("%d %b %Y") if deadline else "",
                "source": "Staff.am",
            })
    return found


# ---------------------------------------------------------------- Remotive
def fetch_remotive(stats):
    found = []
    seen_urls = set()
    for term in REMOTIVE_SEARCHES:
        items = get_json(
            "https://remotive.com/api/remote-jobs", {"search": term}
        ).get("jobs", [])
        for item in items:
            if item.get("url") in seen_urls:
                continue
            seen_urls.add(item.get("url"))
            stats["Remotive"]["fetched"] += 1

            location = item.get("candidate_required_location") or ""
            posted = parse_date(item.get("publication_date"))
            if (
                is_qa(item)
                and is_recent(posted)
                and any(w in location.lower() for w in REMOTIVE_LOCATIONS)
            ):
                found.append({
                    "title": item.get("title", "QA role"),
                    "company": item.get("company_name", "Not specified"),
                    "location": location,
                    "salary": item.get("salary") or "Not specified",
                    "url": item.get("url", ""),
                    "date": posted.isoformat() if posted else "",
                    "source": "Remotive",
                })
    return found


# --------------------------------------------------------------- Arbeitnow
def fetch_arbeitnow(stats):
    found = []
    for page in range(1, ARBEITNOW_PAGES + 1):
        items = get_json(
            "https://www.arbeitnow.com/api/job-board-api", {"page": page}
        ).get("data", [])
        if not items:
            break
        for item in items:
            stats["Arbeitnow"]["fetched"] += 1

            location = item.get("location") or ""
            remote = item.get("remote", False)
            loc = location.lower()
            posted = parse_date(item.get("created_at"))
            if (
                is_qa(item)
                and is_recent(posted)
                and (remote or "armenia" in loc or "yerevan" in loc)
            ):
                found.append({
                    "title": item.get("title", "QA role"),
                    "company": item.get("company_name", "Not specified"),
                    "location": "Remote" if remote else location,
                    "salary": "Not specified",
                    "url": item.get("url", ""),
                    "date": posted.isoformat() if posted else "",
                    "source": "Arbeitnow",
                })
    return found


# ------------------------------------------------------------------ common
def fetch_jobs():
    # Staff.am first, so Armenian jobs are listed first.
    sources = (
        ("Staff.am", fetch_staffam),
        ("Remotive", fetch_remotive),
        ("Arbeitnow", fetch_arbeitnow),
    )
    stats = {name: {"fetched": 0, "matched": 0} for name, _ in sources}
    errors = []
    jobs = []

    for name, fetcher in sources:
        try:
            result = fetcher(stats)
            stats[name]["matched"] = len(result)
            jobs.extend(result)
        except Exception as exc:  # one broken source must not kill the others
            errors.append(f"{name}: {exc}")
            print(f"{name} source failed: {exc!r}")

    unique = {}
    for job in jobs:
        url = job.get("url", "").strip()
        if url.startswith("https://"):
            unique[url] = job

    # Newest first inside each source, then take turns between sources so one
    # source can't fill the whole message.
    by_source = {}
    for job in sorted(unique.values(), key=lambda j: j["date"], reverse=True):
        by_source.setdefault(job["source"], []).append(job)

    ordered = []
    while any(by_source.values()):
        for source_jobs in by_source.values():
            if source_jobs:
                ordered.append(source_jobs.pop(0))
    return ordered, stats, errors


def stats_line(stats):
    return " | ".join(
        f"{name}: {s['fetched']} checked, {s['matched']} matched"
        for name, s in stats.items()
    )


def main():
    try:
        with open(SEEN_FILE, encoding="utf-8") as file:
            seen_list = list(json.load(file))
    except (FileNotFoundError, json.JSONDecodeError):
        seen_list = []
    seen = set(seen_list)

    jobs, stats, errors = fetch_jobs()
    print(stats_line(stats))
    new_jobs = [job for job in jobs if job["url"] not in seen][:MAX_JOBS]

    if not new_jobs:
        lines = ["🔎 Daily QA Jobs", "", "No new matching QA postings today."]
        if errors:
            lines += ["", "⚠️ Some sources failed, so this may be incomplete:"]
            lines += [f"• {e}" for e in errors]
        lines += ["", stats_line(stats)]
        message = "\n".join(lines)
    else:
        lines = ["🔎 Daily QA Jobs", datetime.now().strftime("%d %b %Y"), ""]
        for index, job in enumerate(new_jobs, 1):
            lines.extend([
                f"{index}. {job['title']}",
                f"🏢 {job['company']}",
                f"📍 {job['location']}",
                f"💰 {job['salary']}",
            ])
            if job.get("deadline"):
                lines.append(f"⏳ Deadline: {job['deadline']}")
            lines.extend([
                f"🔗 {job['url']}",
                f"Source: {job['source']}",
                "",
            ])
        if errors:
            lines.append("⚠️ Failed sources: " + "; ".join(errors))
        message = "\n".join(lines).strip()

    message = message[:4000]  # Telegram message size limit

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()

    # Keep insertion order and drop the OLDEST entries.
    for job in new_jobs:
        if job["url"] not in seen:
            seen_list.append(job["url"])
            seen.add(job["url"])
    with open(SEEN_FILE, "w", encoding="utf-8") as file:
        json.dump(seen_list[-500:], file, ensure_ascii=False, indent=2)

    print(f"Sent Telegram post; included {len(new_jobs)} new jobs.")


if __name__ == "__main__":
    main()
