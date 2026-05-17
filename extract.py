#!/usr/bin/env python3
"""Phase 2: Convert page images to markdown via a vision LLM (Ollama / LM Studio / OpenAI-compatible)."""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from langfuse import observe, get_client

DEFAULT_HOST    = os.getenv("LLM_HOST",    "http://192.168.0.58:11434")
DEFAULT_MODEL   = os.getenv("LLM_MODEL",   "qwen3.6:35b")
DEFAULT_API_KEY = os.getenv("LLM_API_KEY", "ollama")
DEFAULT_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "300"))   # seconds — large models on a remote machine can be slow
DEFAULT_RETRIES = 3
ENABLE_REASONING = False  # set True to let Qwen3 use its thinking/reasoning mode

_HERE = Path(__file__).parent

def _load_prompt(filename: str, fallback: str) -> str:
    path = _HERE / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return fallback

SYSTEM_PROMPT = _load_prompt(
    "system_prompt.txt",
    "You are a precise document extraction assistant. Your only job is to convert document page images into clean, accurate markdown. Preserve all headings, lists, tables, figures, captions, and formatting. Do not summarize, interpret, or add commentary.",
)

USER_PROMPT = _load_prompt(
    "user_prompt.txt",
    "Convert this document page to markdown. Reproduce the content exactly as it appears. Output raw markdown only — no code fences, no ```markdown blocks, no commentary before or after.",
)


def image_to_base64(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    with open(path, "rb") as f:
        return mime, base64.b64encode(f.read()).decode()


@observe(name="extract-page")
def extract_page(client, model: str, image_path: Path, retries: int, session_id: str = "") -> str:
    get_client().update_current_trace(
        input=image_path.name,
        session_id=session_id,
        metadata={"model": model},
    )
    mime, b64 = image_to_base64(image_path)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": USER_PROMPT},
            ],
        },
    ]

    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                extra_body={"enable_thinking": ENABLE_REASONING},
            )
            content = response.choices[0].message.content or ""
            if content.strip():
                return content
            print(f"blank response (attempt {attempt}/{retries})", end=" ", flush=True)
        except Exception as e:
            print(f"error: {e} (attempt {attempt}/{retries})", end=" ", flush=True)

        if attempt < retries:
            time.sleep(2)

    return ""


def collect_images(input_dir: Path, limit: int | None) -> list[Path]:
    images = sorted(
        p for p in input_dir.iterdir()
        if p.suffix.lower() in (".png", ".jpg", ".jpeg")
    )
    if limit is not None:
        images = images[:limit]
    return images


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert page images to markdown using a vision LLM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=Path("output"),
                        help="Directory containing page images")
    parser.add_argument("--output-dir", type=Path, default=Path("markdown"),
                        help="Directory to write .md files into")
    parser.add_argument("--host", default=DEFAULT_HOST, help="LLM base URL (Ollama or LM Studio)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY,
                        help="API key (use 'ollama' for Ollama, 'lm-studio' for LM Studio)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N images (useful for prompt tuning)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="Request timeout in seconds")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                        help="Retry attempts for blank or failed responses")
    args = parser.parse_args()

    if not args.input_dir.exists():
        print(f"Error: input directory not found: {args.input_dir}", file=sys.stderr)
        sys.exit(1)

    images = collect_images(args.input_dir, args.limit)
    if not images:
        print(f"No images found in {args.input_dir}", file=sys.stderr)
        sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from langfuse.openai import OpenAI
    except ImportError:
        print("Error: openai and langfuse packages are required. Run: pip install openai langfuse", file=sys.stderr)
        sys.exit(1)

    session_id = time.strftime("batch-%Y%m%d-%H%M%S")
    client = OpenAI(base_url=f"{args.host}/v1", api_key=args.api_key, timeout=args.timeout)

    print(f"Host:    {args.host}")
    print(f"Model:   {args.model}")
    print(f"Timeout: {args.timeout}s  Retries: {args.retries}")
    print(f"Pages:   {len(images)}")
    print(f"Output:  {args.output_dir}/")
    print(f"System prompt ({len(SYSTEM_PROMPT)} chars): {SYSTEM_PROMPT[:80].replace(chr(10), ' ')}…")
    print(f"User prompt   ({len(USER_PROMPT)} chars): {USER_PROMPT[:80].replace(chr(10), ' ')}…\n")

    failed = []
    skipped = []
    page_times = []
    total_chars = 0
    batch_start = time.monotonic()

    for n, img_path in enumerate(images, start=1):
        out_path = args.output_dir / (img_path.stem + ".md")
        if out_path.exists():
            print(f"[{n}/{len(images)}] {img_path.name} → skipped (already exists)")
            skipped.append(img_path.name)
            continue

        print(f"[{n}/{len(images)}] {img_path.name} → ", end="", flush=True)
        t0 = time.monotonic()
        markdown = extract_page(client, args.model, img_path, args.retries, session_id=session_id)
        elapsed = time.monotonic() - t0
        page_times.append(elapsed)

        if markdown.strip():
            out_path.write_text(markdown, encoding="utf-8")
            total_chars += len(markdown)
            print(f"{out_path.name} ({len(markdown)} chars, {elapsed:.1f}s)")
        else:
            print(f"FAILED ({elapsed:.1f}s) — delete .md file to retry")
            failed.append(img_path.name)

    batch_elapsed = time.monotonic() - batch_start
    processed = len(page_times)

    print(f"\n{'─' * 60}")
    print(f"Batch summary")
    print(f"{'─' * 60}")
    print(f"  Total time:      {batch_elapsed:.1f}s ({batch_elapsed/60:.1f} min)")
    if processed:
        print(f"  Avg per page:    {sum(page_times)/processed:.1f}s")
        print(f"  Fastest page:    {min(page_times):.1f}s")
        print(f"  Slowest page:    {max(page_times):.1f}s")
    print(f"  Pages processed: {processed}")
    print(f"  Pages skipped:   {len(skipped)}")
    print(f"  Pages failed:    {len(failed)}")
    print(f"  Total chars out: {total_chars:,}")
    if failed:
        print(f"\n  Failed pages: {', '.join(failed)}")

    get_client().flush()


if __name__ == "__main__":
    main()
