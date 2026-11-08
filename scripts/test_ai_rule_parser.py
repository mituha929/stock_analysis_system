"""Opt-in live Ollama check; no API lifespan, database or Capital DLL startup."""
import argparse
import json
from pathlib import Path
import sys

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.ai.ollama_client import AIError, OllamaClient, OllamaConfig
from app.ai.rule_parser import parse_rule
from app.ai.schemas import ParseRequest


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", action="append", required=True)
    parser.add_argument("--interval", default="day", choices=["1m", "5m", "15m", "60m", "day", "week", "month"])
    args = parser.parse_args()
    load_dotenv(PROJECT / ".env")
    failed = False
    for text in args.text:
        try:
            result = parse_rule(ParseRequest(text=text, history_interval=args.interval), OllamaClient(OllamaConfig.from_env()))
            print(result.model_dump_json(indent=2), flush=True)
        except AIError as exc:
            print(json.dumps({"original_text": text, **exc.detail()}, ensure_ascii=False), flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
