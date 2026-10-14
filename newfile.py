import argparse
import json
import os
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ================= CONFIGURATION =================

BASE_URL = "https://daleel.admission.gov.sd/result2026/Result_2026.aspx"

START_INDEX = 11044052
END_INDEX = START_INDEX + 50

RESULTS_FILE = "results.jsonl"
FAILED_FILE = "failed.txt"
PROGRESS_FILE = "progress.json"

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

    for field in form.select("input[type='hidden'][name]"):
        if field.get("name") == "__VIEWSTATE":
            break

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

            if not result["message"] and not result["tables"]:
                return None, "unrecognized_response"

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
        f"Speed: {speed:.2f}/s | "
        f"ETA: {eta:.1f}s",
        end="",
        flush=True,
    )


# ================= MAIN =================

def main():
    if not 0 <= START_INDEX <= END_INDEX <= 99_999_999:
        raise SystemExit("Invalid application-number range.")

    total = END_INDEX - START_INDEX + 1
    completed = load_completed()

    pending = [
        f"{index:08d}"
        for index in range(START_INDEX, END_INDEX + 1)
        if f"{index:08d}" not in completed
    ]

    print(f"Server: {BASE_URL}")
    print(f"Range: {START_INDEX:08d} - {END_INDEX:08d}")
    print(f"Already saved: {total - len(pending)}")
    print(f"Remaining: {len(pending)}")

    stats = {
        "checked": 0,
        "found": 0,
        "failed": 0,
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
        print(f"Failed: {stats['failed']}")
        print(f"Elapsed: {elapsed:.2f}s")


if __name__ == "__main__":
    main()