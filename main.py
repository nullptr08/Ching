import argparse
import csv
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

# Target URL
URL = "https://daleel.admission.gov.sd/result2026/Result_2026.aspx

# Thread-safe locks
print_lock = threading.Lock()
csv_lock = threading.Lock()
counter_lock = threading.Lock()

def fetch_student_record(form_id, session):
    """Fetches and parses the record for a single 8-digit ID string."""
    form_number = str(form_id).zfill(8)
    start_time = time.time()

    try:
        response = session.get(URL, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        viewstate = soup.find("input", {"id": "__VIEWSTATE"})
        eventvalidation = soup.find("input", {"id": "__EVENTVALIDATION"})
        txt_captcha_elem = soup.find("input", {"id": "txtCaptcha"})

        captcha_code = txt_captcha_elem["value"] if txt_captcha_elem and txt_captcha_elem.get("value") else "12345"

        payload = {
            "__VIEWSTATE": viewstate["value"] if viewstate else "",
            "__EVENTVALIDATION": eventvalidation["value"] if eventvalidation else "",
            "DropDownList1": "1",
            "TextBox1": form_number,
            "txtCaptcha": captcha_code,
            "CaptchaInput": captcha_code,
            "Button1": "عرض النتيجة"
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": URL,
            "Content-Type": "application/x-www-form-urlencoded"
        }

        post_response = session.post(URL, data=payload, headers=headers, timeout=10)
        post_response.raise_for_status()

        latency = time.time() - start_time

        response_soup = BeautifulSoup(post_response.text, "html.parser")
        message = response_soup.find("span", {"id": "LabelMessage"})

        if message and message.text.strip():
            return False, None, None, latency

        student_name_elem = response_soup.find("span", {"id": "StudentNameLabel"})
        faculty_elem = response_soup.find("span", {"id": "FacultyNameLabel"})

        if student_name_elem and faculty_elem:
            return True, student_name_elem.text.strip(), faculty_elem.text.strip(), latency

        return False, None, None, latency

    except Exception:
        return False, None, None, 2.0

def main():
    parser = argparse.ArgumentParser(description="Adaptive Batch Scraper with Auto-Stop")
    parser.add_argument("start", type=int, help="Start index (first valid ID)")
    parser.add_argument("--batch-size", type=int, default=100000, help="IDs per batch (default: 100000)")
    parser.add_argument("--stop-threshold", type=int, default=10000, help="Consecutive missing IDs to trigger auto-stop (default: 10000)")
    parser.add_argument("--output", type=str, default="scraped_results.csv", help="Output CSV filename")
    args = parser.parse_args()

    start_idx = args.start
    batch_size = args.batch_size
    stop_threshold = args.stop_threshold
    csv_filename = args.output

    print(f"[*] Starting scraper from ID {start_idx} with batch size {batch_size}")
    print(f"[*] Auto-stop trigger: {stop_threshold} consecutive missing IDs.\n")

    file_exists = os.path.exists(csv_filename)
    with open(csv_filename, mode="a", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        if not file_exists:
            writer.writerow(["Form ID", "Student Name", "Faculty"])

    current_threads = 10
    min_threads = 5
    max_threads = 25

    thread_local = threading.local()
    def get_thread_session():
        if not hasattr(thread_local, "session"):
            thread_local.session = requests.Session()
        return thread_local.session

    current_id = start_idx
    consecutive_missing = 0
    total_found = 0

    while True:
        batch_end = current_id + batch_size - 1
        print(f"\n--- Processing Batch: {str(current_id).zfill(8)} to {str(batch_end).zfill(8)} ---")
        
        batch_ids = list(range(current_id, batch_end + 1))
        batch_latencies = []
        batch_found_in_this_run = 0

        # Process sub-chunks for adaptive thread adjustment
        sub_chunk_size = 50
        stop_job = False

        for i in range(0, len(batch_ids), sub_chunk_size):
            sub_chunk = batch_ids[i:i + sub_chunk_size]

            with ThreadPoolExecutor(max_workers=current_threads) as executor:
                future_to_id = {
                    executor.submit(fetch_student_record, form_id, get_thread_session()): form_id
                    for form_id in sub_chunk
                }

                for future in as_completed(future_to_id):
                    form_id = future_to_id[future]
                    try:
                        is_valid, name, faculty, latency = future.result()
                        batch_latencies.append(latency)

                        with counter_lock:
                            if is_valid:
                                total_found += 1
                                batch_found_in_this_run += 1
                                consecutive_missing = 0  # Reset streak
                                with print_lock:
                                    print(f"[FOUND] ID: {str(form_id).zfill(8)} | Name: {name} | Faculty: {faculty}")
                                with csv_lock:
                                    with open(csv_filename, mode="a", newline="", encoding="utf-8") as f:
                                        writer = csv.writer(f)
                                        writer.writerow([str(form_id).zfill(8), name, faculty])
                            else:
                                consecutive_missing += 1

                        # Check if we hit the consecutive missing threshold
                        if consecutive_missing >= stop_threshold:
                            print(f"\n[!] Reached {stop_threshold} consecutive missing IDs. Target database end reached.")
                            stop_job = True
                            break

                    except Exception:
                        pass

                if stop_job:
                    break

            # Adapt thread count based on chunk latency
            if batch_latencies:
                avg_latency = sum(batch_latencies[-sub_chunk_size:]) / min(len(batch_latencies), sub_chunk_size)
                if avg_latency < 0.3 and current_threads < max_threads:
                    current_threads = min(max_threads, current_threads + 2)
                elif avg_latency > 0.8 and current_threads > min_threads:
                    current_threads = max(min_threads, current_threads - 3)

        if stop_job:
            break

        print(f"[*] Batch complete. Found in batch: {batch_found_in_this_run} | Current consecutive missing count: {consecutive_missing}/{stop_threshold}")
        current_id = batch_end + 1

    print(f"\n[SUCCESS] Scraping completed. Total records found: {total_found}")

if __name__ == "__main__":
    main()
    