"""Entry point for the scheduled workflow: `python orchestrator.py`."""
import sys

from pipeline import config, telemetry
from pipeline.run import run


def main() -> int:
    settings = config.load()
    if not (settings.db_ready and settings.openai_api_key and settings.serper_api_key):
        print("ERROR: Missing one or more required environment variables "
              "(user, password, host, port, dbname, OPENAI_API_KEY, SERPER_API_KEY).")
        return 1
    try:
        summary = run(settings)
    finally:
        telemetry.shutdown()
    print(f"[pipeline] Finished: {summary['articles']} new headlines, {summary['candidates']} deal candidates, "
          f"outcomes {summary['outcomes']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
