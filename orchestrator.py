# orchestrator.py
import os
import sys
import subprocess

# --- Configuration ---
# List of files to be committed back to the repo if the run is successful
FILES_TO_COMMIT = ["articles.json", "last_article_id.txt"]
# Temporary file to be deleted at the end of the run
TEMP_FILE_TO_DELETE = "classified_articles.json"

def run_step(command: list[str]):
    """
    Runs a command as a subprocess, checks for errors, and exits if a step fails.
    Using a list of arguments is safer than a single command string.
    """
    print(f"\n{'='*20}\n[Orchestrator] Running step: {' '.join(command)}\n{'='*20}")
    try:
        # We use check=True to automatically raise an exception if the process returns a non-zero exit code
        subprocess.run(command, check=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"[Orchestrator] ERROR: Step failed with exit code {e.returncode}.")
        sys.exit(e.returncode)
    except FileNotFoundError:
        print(f"[Orchestrator] ERROR: Command '{command[0]}' not found. Make sure it's in your PATH.")
        sys.exit(1)

def main():
    """
    Main orchestration logic that controls the entire pipeline.
    """
    try:
        # --- Step 1: Check for new articles ---
        # This script will create 'articles.json' only if new articles are found.
        run_step(["python", "check_and_fetch.py"])

        # --- Step 2: Conditional Check ---
        # The key logic: proceed only if the fetch step produced the output file.
        if not os.path.exists("articles.json"):
            print("\n[Orchestrator] No new articles found. Halting workflow gracefully.")
            return # Exit the main function successfully

        print("\n[Orchestrator] New articles found. Proceeding with classification and processing.")

        # --- Step 3: Classify and Process ---
        run_step(["python", "classify_articles.py"])
        run_step(["python", "extractor_uploader.py"])

        # --- Step 4: Commit results back to the repository ---
        # This part is only reached if all previous steps were successful.
        print("\n[Orchestrator] Committing updated articles and state file...")
        run_step(["git", "config", "--global", "user.name", "github-actions[bot]"])
        run_step(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"])
        run_step(["git", "add"] + FILES_TO_COMMIT)
        
        # Use a separate command for commit to handle the case where there are no changes.
        # This command will exit with 0 even if there's nothing to commit.
        subprocess.run(
            ["git", "commit", "-m", "Update with new articles from $(date -u +'%Y-%m-%d')"],
            shell=True, # shell=True is needed for the date command to work
            check=False 
        )
        run_step(["git", "push"])

    finally:
        # --- Step 5: Cleanup ---
        # This 'finally' block ensures that cleanup happens even if a step fails mid-process.
        if os.path.exists(TEMP_FILE_TO_DELETE):
            print(f"\n[Orchestrator] Cleaning up temporary file: {TEMP_FILE_TO_DELETE}")
            os.remove(TEMP_FILE_TO_DELETE)

if __name__ == "__main__":
    main()
    print("\n[Orchestrator] Workflow finished successfully.")
