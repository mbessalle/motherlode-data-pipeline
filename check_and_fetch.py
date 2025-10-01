# check_and_fetch.py
import os
import json
import requests
from datetime import datetime, timedelta, timezone

# --- Configuration ---
API_URL = "https://mininghub.com/api/articles"
STATE_FILE = "last_article_id.txt"
OUTPUT_FILE = "articles.json"

def run():
    """
    Fetches articles for the current day from the MiningHub API, compares them
    to the last processed article, and saves only the new ones.
    """
    last_processed_id = ""
    try:
        with open(STATE_FILE, 'r') as f:
            last_processed_id = f.read().strip()
        print(f"[Fetcher] Last processed article ID: {last_processed_id}")
    except FileNotFoundError:
        print("[Fetcher] State file not found. Will process all of today's articles as new.")

    # --- Prepare Headers and Payload ---
    headers = {
        "accept": "*/*", "content-type": "application/json",
        "origin": "https://mininghub.com", "referer": "https://mininghub.com/articles",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
    }
    
    # Dynamically set the date range for the current day in UTC
    now_utc = datetime.now(timezone.utc)
    start_of_today_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    
    payload = {
        "lastIds": {}, "commodities": {"data": [], "options": {"anyAll": "any"}},
        "daterange": {
            "startDate": start_of_today_utc.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            "endDate": now_utc.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
            "timezoneOffset": -120, "option": "custom"
        },
        "deposits": {"data": [], "options": {"anyAll": "any"}}, "marketcap": [None, None],
        "nsr": {"data": ["Any"]}, "outstandingShares": [None, None], "stages": {"data": []},
        "tags": {
            "data": ["Mergers & Acquisitions", "Project Acquisition"],
            "options": {"anyAll": "any"}
        }
    }

    # --- Fetch All of Today's Articles from the API ---
    all_todays_articles = []
    seen_ids = set()
    
    print(f"[Fetcher] Fetching articles from {payload['daterange']['startDate']} to {payload['daterange']['endDate']}")
    
    # Loop up to 3 times to handle pagination if there are many articles today
    for i in range(3):
        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json().get('data', [])

            if not data:
                print(f"[Fetcher] API call {i+1} returned no articles. Fetching complete.")
                break

            print(f"[Fetcher] API call {i+1} returned {len(data)} articles.")
            
            # Add unique articles to our list for today
            for article in data:
                if article['id'] not in seen_ids:
                    all_todays_articles.append(article)
                    seen_ids.add(article['id'])

            # Prepare for the next potential call by updating the endDate
            oldest_article_date = datetime.fromisoformat(data[-1]['date'].replace('Z', '+00:00'))
            next_end_date = oldest_article_date - timedelta(seconds=1)
            payload['daterange']['endDate'] = next_end_date.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        except requests.RequestException as e:
            print(f"[Fetcher] ERROR: API request failed: {e}")
            return # Exit gracefully if the API fails

    if not all_todays_articles:
        print("[Fetcher] No articles found for today's date range.")
        return

    # --- Compare with State to Find Genuinely New Articles ---
    new_articles_to_save = []
    # The API returns articles newest first, which is perfect for this check.
    for article in all_todays_articles:
        if str(article['id']) == last_processed_id:
            print("[Fetcher] Found the last processed article. Stopping comparison.")
            break
        new_articles_to_save.append(article)

    # --- Save Results and Update State if New Articles Were Found ---
    if new_articles_to_save:
        newest_article_id = str(new_articles_to_save[0]['id'])
        print(f"[Fetcher] Found {len(new_articles_to_save)} new articles. Newest ID is {newest_article_id}.")

        # Reverse the list so the orchestrator processes them in chronological order
        new_articles_to_save.reverse()
        
        # Save the new articles to the output file for the next step
        final_data = {
            "status": "success",
            "total_articles": len(new_articles_to_save),
            "data": new_articles_to_save
        }
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=2)
        print(f"[Fetcher] Saved new articles to {OUTPUT_FILE}.")

        # Update the state file with the ID of the newest article from this run
        with open(STATE_FILE, 'w') as f:
            f.write(newest_article_id)
        print(f"[Fetcher] Updated state file with ID: {newest_article_id}.")
    else:
        print("[Fetcher] No new articles since the last run.")

if __name__ == "__main__":
    run()
