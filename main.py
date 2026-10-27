import json
import os
from datetime import datetime, timedelta, timezone

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen_jobs.json"
MAX_JOBS = 5
HEADERS = {"User-Agent": "DailyQAJobsBot/1.0"}

QA_WORDS = ("qa", "quality assurance", "software tester",
            "manual tester", "test engineer", "software testing")

def is_qa(job):
    title = job.get("title", "").lower()
    return any(word in title for word in QA_WORDS)

def is_recent(date_text):
    if not date_text:
        return True
    try:
        date_text = date_text.replace("Z", "+00:00")
        posted = datetime.fromisoformat(date_text)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return posted >= datetime.now(timezone.utc) - timedelta(days=30)
    except (ValueError, TypeError):
        return True

def fetch_jobs():
    jobs = []

    # Remote jobs from Remotive
    try:
        response = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": "QA"},
            headers=HEADERS,
            timeout=25,
        )
        response.raise_for_status()

        for item in response.json().get("jobs", []):
            location = item.get("candidate_required_location", "")
            if (
                is_qa(item)
                and is_recent(item.get("publication_date"))
                and location
                and any(word in location.lower() for word in
                        ("worldwide", "anywhere", "europe", "armenia"))
            ):
                jobs.append({
                    "title": item.get("title", "QA role"),
                    "company": item.get("company_name", "Not specified"),
                    "location": location,
                    "salary": item.get("salary") or "Not specified",
                    "url": item.get("url", ""),
                    "date": item.get("publication_date", ""),
                    "source": "Remotive",
                })
    except Exception as exc:
        print(f"Remotive source unavailable: {exc}")

    # Jobs from Arbeitnow, including remote and Armenia-based roles
    try:
        response = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            headers=HEADERS,
            timeout=25,
        )
        response.raise_for_status()

        for item in response.json().get("data", []):
            location = item.get("location", "")
            remote = item.get("remote", False)
            searchable_location = location.lower()

            if (
                is_qa(item)
                and is_recent(item.get("created_at"))
                and (
                    remote
                    or "armenia" in searchable_location
                    or "yerevan" in searchable_location
                )
            ):
                jobs.append({
                    "title": item.get("title", "QA role"),
                    "company": item.get("company_name", "Not specified"),
                    "location": "Remote" if remote else location,
                    "salary": "Not specified",
                    "url": item.get("url", ""),
                    "date": item.get("created_at", ""),
                    "source": "Arbeitnow",
                })
    except Exception as exc:
        print(f"Arbeitnow source unavailable: {exc}")

    unique = {}
    for job in jobs:
        url = job.get("url", "").strip()
        if url.startswith("https://"):
            unique[url] = job

    return sorted(
        unique.values(),
        key=lambda job: job.get("date", ""),
        reverse=True,
    )

def main():
    try:
        with open(SEEN_FILE, encoding="utf-8") as file:
            seen = set(json.load(file))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = set()

    jobs = fetch_jobs()
    new_jobs = [job for job in jobs if job["url"] not in seen][:MAX_JOBS]

    if not new_jobs:
        message = (
            "🔎 Daily QA Jobs\n\n"
            "No new matching QA postings were found today. "
            "I'll check again tomorrow."
        )
    else:
        lines = ["🔎 Daily QA Jobs", datetime.now().strftime("%d %b %Y"), ""]

        for index, job in enumerate(new_jobs, 1):
            lines.extend([
                f"{index}. {job['title']}",
                f"🏢 {job['company']}",
                f"📍 {job['location']}",
                f"💰 {job['salary']}",
                f"🔗 {job['url']}",
                f"Source: {job['source']}",
                "",
            ])

        message = "\n".join(lines).strip()

    # Telegram messages have a size limit.
    message = message[:4000]

    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()

    seen.update(job["url"] for job in new_jobs)
    with open(SEEN_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen)[-500:], file, ensure_ascii=False, indent=2)

    print(f"Sent Telegram post; included {len(new_jobs)} new jobs.")

if __name__ == "__main__":
    main()
