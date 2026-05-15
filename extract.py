#!/usr/bin/env python3
"""Phase 2: Convert page images to markdown via a vision LLM (LM Studio / OpenAI-compatible)."""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

DEFAULT_HOST = "http://192.168.0.58:1234"
DEFAULT_MODEL = "qwen3.6-35b-a3b"
DEFAULT_TIMEOUT = 300   # seconds — large models on a remote machine can be slow
DEFAULT_RETRIES = 3
ENABLE_REASONING = False  # set True to let Qwen3 use its thinking/reasoning mode

SYSTEM_PROMPT = "You are a precise document extraction assistant. Your only job is to convert document page images into clean, accurate markdown. Preserve all headings, lists, tables, figures, captions, and formatting. Do not summarize, interpret, or add commentary."

USER_PROMPT = "Convert this document page to markdown. Reproduce the content exactly as it appears. Output raw markdown only — no code fences, no ```markdown blocks, no commentary before or after."


def image_to_base64(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    with open(path, "rb") as f:
        return mime, base64.b64encode(f.read()).decode()


def extract_page(client, model: str, image_path: Path, retries: int) -> str:
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
    parser.add_argument("--host", default=DEFAULT_HOST, help="LM Studio base URL")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Model name as shown in LM Studio")
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
        from openai import OpenAI
    except ImportError:
        print("Error: openai package not installed. Run: pip install openai", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(base_url=f"{args.host}/v1", api_key="lm-studio", timeout=args.timeout)

    print(f"Host:    {args.host}")
    print(f"Model:   {args.model}")
    print(f"Timeout: {args.timeout}s  Retries: {args.retries}")
    print(f"Pages:   {len(images)}")
    print(f"Output:  {args.output_dir}/\n")

    failed = []
    for n, img_path in enumerate(images, start=1):
        out_path = args.output_dir / (img_path.stem + ".md")
        if out_path.exists():
            print(f"[{n}/{len(images)}] {img_path.name} → skipped (already exists)")
            continue

        print(f"[{n}/{len(images)}] {img_path.name} → ", end="", flush=True)
        markdown = extract_page(client, args.model, img_path, args.retries)

        if markdown.strip():
            out_path.write_text(markdown, encoding="utf-8")
            print(f"{out_path.name} ({len(markdown)} chars)")
        else:
            print("FAILED — skipped (delete output file to retry)")
            failed.append(img_path.name)

    print(f"\nDone. {len(images) - len(failed)} page(s) written to {args.output_dir}/")
    if failed:
        print(f"Failed ({len(failed)}): {', '.join(failed)}")


if __name__ == "__main__":
    main()
