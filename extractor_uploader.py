import json
import os
import openai
import psycopg2
import psycopg2.extras
import psycopg2.sql
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Error

# --- 1. Configuration ---
load_dotenv()
DB_USER = os.getenv("user")
DB_PASSWORD = os.getenv("password")
DB_HOST = os.getenv("host")
DB_PORT = os.getenv("port")
DB_NAME = os.getenv("dbname")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not all([DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME, OPENAI_API_KEY]):
    print("ERROR: Missing one or more required environment variables.")
    exit()

client = openai.OpenAI(api_key=OPENAI_API_KEY)

# --- 2. THE DEFINITIVE "INTELLIGENT ANALYST" PROMPT ---
# PASTE YOUR FULL, DETAILED SYSTEM PROMPT FOR THE ADVANCED LLM HERE
SYSTEM_PROMPT = """
You are a meticulous and highly precise financial analyst for the mining industry, specializing in Mergers and Acquisitions (M&A). Your task is to analyze a news article and generate a structured JSON output for a database update. You must follow a strict cognitive workflow and prove your understanding by referencing the provided database schema.

**YOUR FINAL OUTPUT MUST BE A SINGLE JSON OBJECT WITH TWO TOP-LEVEL KEYS: "reasoning_and_acknowledgement" and "database_updates".**

<cognitive_workflow>

**STEP 1: ACKNOWLEDGE AND INTERNALIZE RULES (MANDATORY)**
- In the `reasoning_and_acknowledgement` section of your output, create a key called `rules_acknowledged`.
- In this key, you must summarize your understanding of the most critical rules by referencing the schema below:
  1.  **Data Types & Schema:** Acknowledge that you will strictly follow the data types (numeric, text, date) for each field as described in the Target Database Schema. State that numeric fields must be a number or null.
  2.  **Entity Resolution:** State that you must identify the BUYER and SELLER, and that `owner_name` (the company buying the project) must be updated to the BUYER in a sale.
  3.  **Aggregation:** Confirm that you will sum all payment components (upfront, milestone, etc.) for fields like `cash_payments_value`.
  4.  **Ambiguity:** State that if a numeric value (like the monetary value of shares) is not calculable from the text, the corresponding numeric field in the schema must be `null`.

**STEP 2: EXECUTE ANALYSIS**
- In the `reasoning_and_acknowledgement` section, create a key called `analysis_steps`.
- Follow this process and document your findings here:
  1.  **Entity Resolution:** State the identified BUYER and SELLER.
  2.  **Deal Deconstruction:** List every cash payment component found. List every share payment component found.
  3.  **Calculations:** Show the summation for `cash_payments_value`. State the final `share_price` found. Explain why `shares_value` is either a calculated number or `null`.
  4.  **Final Check:** Briefly confirm your findings align with the rules and the schema.

**STEP 3: GENERATE CLEAN DATABASE JSON**
- In the `database_updates` section of your output, create the final, clean JSON object.
- This object must contain ONLY the database fields and their final, correctly formatted values, adhering strictly to the schema.

</cognitive_workflow>

---
**Target Database Schema (FULL) - YOU MUST ADHERE TO THIS**
- project_name: (text) The name of the acquired project(s).
- primary_commodity: (text) The main chemical symbol of the primary commodity (e.g., Au, Cu, Fe).
- commodities: (text) A comma-separated list of other associated elements/metals.
- resource_value_scraped: (text) The declared mineral resource statement (follow the critical rule below).
- stage: (text) Internal stage label (DO NOT POPULATE, leave for manual input).
- stage_mining_hub: (text) The stage classification from Mining Hub, if the source is Mining Hub.
- stage_scrapped: (text) The project's development level as described in the news (e.g., Exploration, Advanced Stage, Mining).
- project_state_or_province: (text) The state or province where the project is located.
- project_country: (text) The country where the project is located.
- longitude: (numeric) Geographic longitude in decimal degrees.
- latitude: (numeric) Geographic latitude in decimal degrees.
- project_area: (numeric) The project's surface area.
- project_area_unit: (text) The unit for the project area (e.g., ha, km2).
- owner_name: (text) The company buying the project (the BUYER).
- owner_ticker: (text) The stock ticker of the buying company.
- owner_marketcap: (numeric) The market capitalization of the buying company.
- news_link: (text) The URL of the scraped news article.
- news_title: (text) The title of the news article.
- deal_type: (text) The type of deal announced (e.g., option agreement, earn-in agreement, JV, sale).
- deal_period: (text) The duration of the deal (e.g., "4 years").
- interest_acquired_percent: (numeric) The percentage of interest acquired in the deal.
- aggregate_deal_currency: (text) The currency of the aggregate deal value (3-letter ISO code).
- aggregate_deal_value: (numeric) The total sum of all payments in the deal.
- cash_payments_currency: (text) The currency of the cash payments (3-letter ISO code).
- cash_payments_value: (numeric) The total sum of all cash payments.
- exp_comit_currency: (text) The currency of exploration expenditure commitments (3-letter ISO code).
- exp_comit_value: (numeric) The total sum of exploration expenditure commitments.
- drilling_amount: (numeric) Total meters committed to be drilled.
- drilling_value: (numeric) The estimated cost of drilling or the reported value.
- shares_currency: (text) The currency used to value the share payment (3-letter ISO code).
- shares_value: (numeric) The equivalent value paid in shares.
- shares_value_scraped: (numeric) The equivalent value paid in shares if explicitly stated in the news.
- shares_amount: (numeric) The number of shares used in the payment.
- share_price: (numeric) The price per share used for the valuation.
- nsr_percent: (numeric) The reported Net Smelter Royalty (NSR) or GRR as a percentage.
- news_date: (date) The date of the news article in YYYY-MM-DD format.
---

**ADDITIONAL RULES TO APPLY DURING ANALYSIS:**
- **DATA RELEVANCE:** Only extract data for the primary project of interest.
- **SHARED COMMITMENTS:** If a value is shared between N projects, use `VALUE / N`.
- **RESOURCE RULE:** `resource_value_scraped` is for mass/volume only (e.g., 'tonnes', 'ounces'), starting with a number. It must be `null` if no resource is stated.
- **EXPLORATION COMMITMENTS:** If given in meters, convert to USD (1 meter = 250 USD).
"""

# --- 3. GENERALIZED Database Functions ---

def fetch_single_project_record(connection, table_name, project_name):
    """Fetches a single project record from a DYNAMICALLY specified table."""
    record = None
    query = psycopg2.sql.SQL("SELECT * FROM {} WHERE project_name = %s").format(
        psycopg2.sql.Identifier(table_name)
    )
    cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
    try:
        cursor.execute(query, (project_name,))
        row = cursor.fetchone()
        if row:
            record = dict(row)
    finally:
        cursor.close()
    return record

def update_project_in_db(connection, table_name, project_name, updates):
    """Updates a project in the specified continent table."""
    if not updates: return
    for key, value in updates.items():
        if value == "": updates[key] = None
    
    set_clauses = [psycopg2.sql.SQL("{} = %s").format(psycopg2.sql.Identifier(key)) for key in updates.keys()]
    
    sql_query = psycopg2.sql.SQL("UPDATE {} SET {} WHERE project_name = %s").format(
        psycopg2.sql.Identifier(table_name),
        psycopg2.sql.SQL(', ').join(set_clauses)
    )
    
    values = list(updates.values()) + [project_name]
    with connection.cursor() as cursor:
        cursor.execute(sql_query, values)
        connection.commit()

def add_project_to_db(connection, table_name, project_data):
    """Inserts a new project into the specified continent table."""
    if not project_data or 'project_name' not in project_data:
        print("   -> Skipping add: project_name is missing.")
        return
    
    for key, value in project_data.items():
        if value == "": project_data[key] = None
    
    columns = project_data.keys()
    values = project_data.values()
    
    sql_query = psycopg2.sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        psycopg2.sql.Identifier(table_name),
        psycopg2.sql.SQL(', ').join(map(psycopg2.sql.Identifier, columns)),
        psycopg2.sql.SQL(', ').join(psycopg2.sql.SQL('%s') for _ in values)
    )
    
    with connection.cursor() as cursor:
        cursor.execute(sql_query, list(values))
        connection.commit()

# --- 4. Scrape & Analyze Functions ---

def scrape_article_with_playwright(page, url):
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        main_content = page.locator('article').first or page.locator('main').first
        text = main_content.inner_text() if main_content.is_visible() else page.locator('body').inner_text()
        return ' '.join(text.split())
    except Error as e:
        print(f"   -> Playwright failed to scrape {url}. Error: {e}")
        return None

def analyze_content_with_llm(article_text, project_record):
    existing_data_str = "\n".join([f"- {key}: {value}" for key, value in project_record.items() if value is not None])
    user_prompt = f"""
    **Primary Project of Interest:** '{project_record.get('project_name')}'
    **Existing Database Record (if any):**
    {existing_data_str}
    **News Article Content:**
    ---
    {article_text[:15000]}
    ---
    **Task:**
    Execute your cognitive workflow. Produce the mandatory two-part JSON object containing your reasoning and the final database updates.
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"}
        )
        response_str = response.choices[0].message.content
        response_obj = json.loads(response_str)
        database_updates = response_obj.get("database_updates", {})
        print(f"   -> LLM analysis complete.")
        return database_updates
    except Exception as e:
        print(f"   -> LLM analysis or parsing failed. Error: {e}")
        return {}

# --- 5. Main Execution ---
if __name__ == "__main__":
    articles_json_file = 'classified_articles.json'
    connection = None
    try:
        connection = psycopg2.connect(
            user=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=DB_PORT, dbname=DB_NAME
        )
        print("Database connection successful!")
        
        articles_to_process = []
        try:
            with open(articles_json_file, 'r', encoding='utf-8') as f:
                articles_to_process = json.load(f).get('data', [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error loading {articles_json_file}: {e}")
            print("Please run the 'classify_articles.py' script first.")
            exit()
        
        updates_made_count = 0
        inserts_made_count = 0
        print(f"\n--- Found {len(articles_to_process)} classified articles to process. Starting execution. ---\n")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for article in articles_to_process:
                classification = article['classification']
                status = classification['status']
                
                if status == 'unclassified':
                    project_name_for_log = article.get('project_name', 'Unknown')
                    print(f"-> Skipping '{project_name_for_log}': Article is marked as unclassified.")
                    print("-" * 20)
                    continue

                table_name = classification['table_name']
                project_name = article['project_name']
                
                print(f"-> Processing '{project_name}' for table '{table_name}' (Status: {status})")

                link = article.get('link')
                if not link:
                    print("   -> Skipping: No link provided.\n")
                    continue

                project_record_for_llm = {}
                if status == 'existing':
                    project_record_for_llm = fetch_single_project_record(connection, table_name, project_name)
                    if not project_record_for_llm:
                        print(f"   -> WARNING: Classified as 'existing' but not found in DB. Treating as 'new'.")
                        status = 'new'
                
                if status == 'new':
                    project_record_for_llm = {'project_name': project_name}
                
                page = None
                try:
                    page = browser.new_page()
                    content = scrape_article_with_playwright(page, link)
                    if not content:
                        print("   -> Skipping: Could not retrieve article content.\n")
                        continue
                    
                    print("   -> Analyzing content with advanced LLM...")
                    llm_extracted_data = analyze_content_with_llm(content, project_record_for_llm)
                    
                    if not llm_extracted_data:
                        print(f"   -> No data extracted by LLM for '{project_name}'.")
                        continue

                    llm_extracted_data['news_link'] = link
                    llm_extracted_data['news_title'] = article.get('title')
                    if article.get('date'):
                        llm_extracted_data['news_date'] = article.get('date').split('T')[0]

                    if status == 'new':
                        new_project_record = {'project_name': project_name}
                        new_project_record.update(llm_extracted_data)
                        print(f"   ✔ Attempting INSERT into '{table_name}'...")
                        try:
                            add_project_to_db(connection, table_name, new_project_record)
                            inserts_made_count += 1
                            print(f"   -> New record added successfully.")
                        except psycopg2.Error as e:
                            print(f"   ❌ DATABASE ERROR on INSERT: {e}")

                    elif status == 'existing':
                        print(f"   ✔ Attempting UPDATE in '{table_name}'...")
                        try:
                            update_project_in_db(connection, table_name, project_name, llm_extracted_data)
                            updates_made_count += 1
                            print(f"   -> Record updated successfully.")
                        except psycopg2.Error as e:
                            print(f"   ❌ DATABASE ERROR on UPDATE: {e}")
                        
                finally:
                    if page: page.close()
                    print("-" * 20)
            browser.close()

        print("\n--- Processing Complete ---")
        print(f"Total new records added: {inserts_made_count}")
        print(f"Total existing records updated: {updates_made_count}")

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    
    finally:
        if connection:
            connection.close()
            print("Database connection closed.")

