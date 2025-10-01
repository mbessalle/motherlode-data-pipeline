import json
import os
import openai
import psycopg2
import psycopg2.sql
from dotenv import load_dotenv

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

# --- 2. Mappings & Prompts ---

# This mapping is reliable: it converts a full continent name to a DB table name.
CONTINENT_TO_TABLE_MAP = {
    "Africa": "africa",
    "Asia": "asia",
    "Europe": "europe",
    "North America": "north_america",
    "Oceania": "oceania",
    "South America": "south_america"
}

CLASSIFIER_SYSTEM_PROMPT = """
You are a highly efficient geographic data classifier for the mining industry. Your task is to identify the project's primary COUNTRY and CONTINENT from a given text snippet.

**RULES:**
1.  Analyze the provided text, which includes tags, title, and a description.
2.  Your primary goal is to determine the country and the continent where the project is located.
3.  The continent MUST be one of the following: Africa, Asia, Europe, North America, Oceania, South America.
4.  Respond with a single JSON object containing two keys: "country" and "continent".
5.  If you cannot confidently determine the country or continent, the corresponding value MUST be `null`.

**Example Input:**
"tags: 'USA|Alaska||Gold|Mergers & Acquisitions', title: 'Nova Minerals makes cornerstone investment in Adelong Gold', description: '...progress its flagship Estelle Gold and Critical Minerals Project in Alaska.'"

**Example Output:**
{"country": "USA", "continent": "North America"}

**Example Input 2:**
"tags: 'Mergers & Acquisitions', title: 'Global Miner Corp Expands Portfolio', description: 'The company announced a significant acquisition today...'"

**Example Output 2:**
{"country": null, "continent": null}
"""

# --- 3. Helper Functions ---

def get_location_from_llm(context: str) -> dict:
    """Uses a lightweight LLM to extract the country and continent."""
    if not context.strip():
        return {"country": None, "continent": None}
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Fast and smart enough for this task
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": context}
            ],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        data = json.loads(response.choices[0].message.content)
        # Ensure the keys are present
        return {
            "country": data.get("country"),
            "continent": data.get("continent")
        }
    except Exception as e:
        print(f"   -> LLM location extraction failed. Error: {e}")
        return {"country": None, "continent": None}

def project_exists_in_table(connection, table_name, project_name):
    """Checks if a project with a given name exists in a specific table."""
    query = psycopg2.sql.SQL("SELECT 1 FROM {} WHERE project_name = %s").format(
        psycopg2.sql.Identifier(table_name)
    )
    with connection.cursor() as cursor:
        cursor.execute(query, (project_name,))
        return cursor.fetchone() is not None

# --- 4. Main Execution ---
if __name__ == "__main__":
    articles_json_file = 'articles.json'
    output_json_file = 'classified_articles.json'
    connection = None
    
    try:
        # Load articles from the source file
        try:
            with open(articles_json_file, 'r', encoding='utf-8') as f:
                articles_to_process = json.load(f).get('data', [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error loading {articles_json_file}: {e}")
            exit()
            
        print(f"--- Starting classification for {len(articles_to_process)} articles. ---")
        
        # Connect to the database
        connection = psycopg2.connect(
            user=DB_USER, password=DB_PASSWORD, host=DB_HOST, port=DB_PORT, dbname=DB_NAME
        )
        print("Database connection successful.")

        classified_articles = []
        unclassified_count = 0
        
        for article in articles_to_process:
            project_name = article.get('project_name')
            
            # STEP 1: Check if project_name exists and is not null
            if not project_name:
                print("-> Skipping article with null project_name.")
                continue

            print(f"\n-> Processing project: '{project_name}'")
            
            # Initialize default classification data
            classification_data = {
                "country": None,
                "continent": None,
                "table_name": None,
                "status": "unclassified",
                "reason": "Initial state"
            }

            # STEP 2: Get continent and table name via LLM
            context_for_llm = (
                f"tags: '{article.get('tags', '')}', "
                f"title: '{article.get('title', '')}', "
                f"description: '{article.get('description', '')}'"
            )
            
            location_data = get_location_from_llm(context_for_llm)
            country = location_data.get("country")
            continent = location_data.get("continent")
            
            classification_data.update({"country": country, "continent": continent})

            if continent and continent in CONTINENT_TO_TABLE_MAP:
                table_name = CONTINENT_TO_TABLE_MAP[continent]
                classification_data["table_name"] = table_name
                print(f"   -> LLM identified continent: '{continent}', targeting table: '{table_name}'")
                
                # STEP 3: Check if project exists in the identified table
                is_existing = project_exists_in_table(connection, table_name, project_name)
                
                if is_existing:
                    classification_data["status"] = "existing"
                    classification_data["reason"] = f"Project found in table '{table_name}'."
                    print(f"   -> STATUS: EXISTING")
                else:
                    classification_data["status"] = "new"
                    classification_data["reason"] = f"Project not found in table '{table_name}', will be added."
                    print(f"   -> STATUS: NEW")
            else:
                if not continent:
                    classification_data["reason"] = "LLM could not determine a valid continent."
                else:
                    classification_data["reason"] = f"LLM returned an invalid continent: '{continent}'."
                print(f"   -> STATUS: UNCLASSIFIED. Reason: {classification_data['reason']}")
                unclassified_count += 1
            
            # STEP 4: Pass additional metadata to the article
            article['classification'] = classification_data
            classified_articles.append(article)
            
        # STEP 5: Store it in the new classified_articles.json
        with open(output_json_file, 'w', encoding='utf-8') as f:
            json.dump({"data": classified_articles}, f, indent=2)

        print("\n--- Classification Complete ---")
        print(f"Successfully processed {len(classified_articles)} valid articles.")
        print(f"Results saved to '{output_json_file}'.")
        print(f"Total unclassified articles: {unclassified_count}")

    except Exception as e:
        print(f"An unexpected error occurred during classification: {e}")
    finally:
        if connection:
            connection.close()
            print("Database connection closed.")

