import os
from dotenv import load_dotenv

def main():
    """Main orchestration function for the data pipeline."""
    print("--- PIPELINE START (Task 1: Hello World) ---")
    
    # Load environment variables from .env file (for local testing)
    # This will do nothing if .env is empty or not present, but it's good practice
    # to include it early.
    load_dotenv() 

    print("Hello from orchestrator.py! The pipeline is ready to build!")
    print(f"Current working directory: {os.getcwd()}")
    print(f"Value of a dummy variable (if set in .env): {os.getenv('DUMMY_VAR', 'Not Set')}") # Example of loading env var

    print("--- PIPELINE END (Task 1: Hello World) ---")

if __name__ == "__main__":
    main()