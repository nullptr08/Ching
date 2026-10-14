import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ================= CONFIGURATION =================

BASE_URL = "https://daleel.admission.gov.sd/result2026/Result_2026.aspx"

# Output files with batch prefix to avoid collisions in parallel jobs
BATCH_ID = os.environ.get('BATCH_ID', '0')
RESULTS_FILE = f"results_batch_{BATCH_ID}.jsonl"
FAILED_FILE = f"failed_batch_{BATCH_ID}.txt"
PROGRESS_FILE = f"progress_batch_{BATCH_ID}.json"

REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
REQUEST_DELAY = 0.5
PROGRESS_EVERY = 5

# ASP.NET form field names
FORM_ID = "form1"
DROPDOWN_FIELD = "DropDownList1"
APPLICATION_FIELD = "TextBox1"
SUBMIT_FIELD = "Button1"

DROPDOWN_VALUE = "1"
SUBMIT_VALUE = "عرض النتيجة"

# Set to True only when the test server explicitly permits
# requests that omit CAPTCHA fields.
OMIT_CAPTCHA = True


# ================= SESSION =================

def create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "University-Authorized-Test/1.0"
    })
    return session


# ================= FORM HANDLING =================

def load_form(session):
    response = session.get(
        BASE_URL,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form", id=FORM_ID)

    if not form:
        raise RuntimeError("ASP.NET results form not found.")

    payload = {}

    for field in form.select('input[type="hidden"][name]'):
        name = field["name"]

        if OMIT_CAPTCHA and name.lower() in {
            "txtcaptcha",
            "shareresultfield",
        }:
            continue

        payload[name] = field.get("value", "")

    action = urljoin(
        response.url,
        form.get("action") or response.url,
    )

    return soup, payload, action


# ================= RESULT PARSING =================

def parse_result(html):
    soup = BeautifulSoup(html, "html.parser")

    message = soup.find(id="LabelMessage")

    tables = []

    for table in soup.find_all("table"):
        rows = []

        for row in table.find_all("tr"):
            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["th", "td"])
            ]

            if cells:
                rows.append(cells)

        if rows:
            tables.append(rows)

    return {
        "message": (
            message.get_text(" ", strip=True)
            if message else ""
        ),
        "tables": tables,
    }


def is_valid_result(result):
    """Check if the parsed result contains actual data (not an error)."""
    message = result.get("message", "").lower()
    tables = result.get("tables", [])
    
    # Error indicators (Arabic and English)
    error_keywords = [
        "لا توجد نتيجة",  # No result
        "غير موجود",       # Not found
        "خطأ",            # Error
        "not found",
        "error",
        "invalid",
        "no result",
        "does not exist"
    ]
    
    # If message contains error keywords, it's invalid
    if any(err in message for err in error_keywords):
        return False
    
    # Valid result must have either:
    # 1. A non-empty message with substance, OR
    # 2. At least one table with data
    has_message = len(message) > 10
    has_tables = len(tables) > 0 and any(len(t) > 0 for t in tables)
    
    return has_message or has_tables


# ================= REQUEST =================

def fetch_result(session, application_id):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _, payload, action = load_form(session)

            payload.update({
                DROPDOWN_FIELD: DROPDOWN_VALUE,
                APPLICATION_FIELD: application_id,
                SUBMIT_FIELD: SUBMIT_VALUE,
            })

            response = session.post(
                action,
                data=payload,
                headers={"Referer": BASE_URL},
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 429:
                return None, "rate_limited"

            if response.status_code >= 500:
                if attempt < MAX_RETRIES:
                    time.sleep(attempt * 2)
                    continue

                return None, "server_error"

            response.raise_for_status()

            result = parse_result(response.text)

            # Check if result is valid (not an error message)
            if not is_valid_result(result):
                return None, "not_found"

            return result, "success"

        except requests.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
                continue

            return None, "network_error"

    return None, "failed"


# ================= STORAGE =================

def load_completed():
    completed = set()

    if not Path(RESULTS_FILE).exists():
        return completed

    with open(RESULTS_FILE, encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
                completed.add(record["application_id"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    return completed


def save_result(application_id, result):
    record = {
        "application_id": application_id,
        **result,
    }

    with open(RESULTS_FILE, "a", encoding="utf-8") as file:
        file.write(
            json.dumps(record, ensure_ascii=False) + "\n"
        )


def save_failure(application_id, reason):
    with open(FAILED_FILE, "a", encoding="utf-8") as file:
        file.write(f"{application_id}\t{reason}\n")


def save_progress(stats):
    temporary = PROGRESS_FILE + ".tmp"

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(stats, file, indent=2)

    os.replace(temporary, PROGRESS_FILE)


# ================= PROGRESS =================

def show_progress(stats, total):
    elapsed = time.perf_counter() - stats["started"]
    speed = stats["checked"] / elapsed if elapsed else 0
    remaining = max(total - stats["checked"], 0)
    eta = remaining / speed if speed else 0

    print(
        f"\rChecked: {stats['checked']}/{total} | "
        f"Found: {stats['found']} | "
        f"Failed: {stats['failed']} | "
        f"Not Found: {stats['not_found']} | "
        f"Speed: {speed:.2f}/s | "
        f"ETA: {eta:.1f}s",
        end="",
        flush=True,
    )


# ================= MAIN =================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Scrape university admission results for a range of application IDs."
    )
    parser.add_argument(
        "start",
        type=int,
        help="Starting application index (inclusive)",
    )
    parser.add_argument(
        "end",
        type=int,
        help="Ending application index (inclusive)",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()

    start_index = args.start
    end_index = args.end

    if not 0 <= start_index <= end_index <= 99_999_999:
        raise SystemExit("Invalid application-number range.")

    total = end_index - start_index + 1
    completed = load_completed()

    pending = [
        f"{index:08d}"
        for index in range(start_index, end_index + 1)
        if f"{index:08d}" not in completed
    ]

    print(f"Server: {BASE_URL}")
    print(f"Batch ID: {BATCH_ID}")
    print(f"Range: {start_index:08d} - {end_index:08d}")
    print(f"Already saved: {total - len(pending)}")
    print(f"Remaining: {len(pending)}")

    if not pending:
        print("No pending IDs to check. Exiting.")
        return

    stats = {
        "checked": 0,
        "found": 0,
        "failed": 0,
        "not_found": 0,
        "started": time.perf_counter(),
    }

    session = create_session()

    try:
        for application_id in pending:
            result, status = fetch_result(
                session,
                application_id,
            )

            stats["checked"] += 1

            if status == "success":
                save_result(application_id, result)
                stats["found"] += 1
            elif status == "not_found":
                stats["not_found"] += 1
                # Don't save "not found" to failed file - it's expected
            else:
                save_failure(application_id, status)
                stats["failed"] += 1

                if status == "rate_limited":
                    print(
                        "\nServer rate limit detected. "
                        "Stopping to avoid further requests."
                    )
                    break

            if stats["checked"] % PROGRESS_EVERY == 0:
                show_progress(stats, len(pending))

                save_progress({
                    key: value
                    for key, value in stats.items()
                    if key != "started"
                })

            time.sleep(REQUEST_DELAY)

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    finally:
        elapsed = time.perf_counter() - stats["started"]
        session.close()

        save_progress({
            key: value
            for key, value in stats.items()
            if key != "started"
        })

        print("\n\nFinished")
        print(f"Checked: {stats['checked']}")
        print(f"Found: {stats['found']}")
        print(f"Not Found: {stats['not_found']}")
        print(f"Failed: {stats['failed']}")
        print(f"Elapsed: {elapsed:.2f}s")


if __name__ == "__main__":
    main()
    