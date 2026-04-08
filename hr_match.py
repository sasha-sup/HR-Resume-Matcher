#!/usr/bin/env python3
"""HR Resume Matcher — console app powered by local Claude agent."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.parse
import urllib.request

import PyPDF2
from docx import Document

MAX_INPUT_CHARS = 15_000
DEFAULT_RESUME_URL = "https://sashasup.link/cv/devops_latest.pdf"


# ── Claude integration ───────────────────────────────────────────────────────

def ask_claude(prompt: str) -> str:
    if not shutil.which("claude"):
        print("Error: 'claude' binary not found in PATH. Install Claude Code first.", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        ["claude", "--print", prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude error: {result.stderr.strip()}")
    return result.stdout.strip()


# ── File parsers ─────────────────────────────────────────────────────────────

def _read_pdf(path: str) -> str:
    pages = []
    with open(path, "rb") as f:
        for page in PyPDF2.PdfReader(f).pages:
            pages.append(page.extract_text() or "")
    text = "\n".join(pages)
    if len(text) > MAX_INPUT_CHARS:
        print(f"Warning: PDF text truncated to {MAX_INPUT_CHARS} characters.", file=sys.stderr)
        text = text[:MAX_INPUT_CHARS]
    return text


def _read_docx(path: str) -> str:
    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    if len(text) > MAX_INPUT_CHARS:
        print(f"Warning: DOCX text truncated to {MAX_INPUT_CHARS} characters.", file=sys.stderr)
        text = text[:MAX_INPUT_CHARS]
    return text


def _download_to_tmp(url: str) -> str:
    """Download a URL to a temporary file and return the path."""
    suffix = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".pdf"
    print(f"Downloading {url} ...", file=sys.stderr)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        urllib.request.urlretrieve(url, tmp.name)
    except Exception as e:
        os.unlink(tmp.name)
        raise RuntimeError(f"Failed to download {url}: {e}")
    return tmp.name


def parse_source(path_or_text: str) -> str:
    """If the argument is a URL — download first.
    If it's a path to an existing file — read it.
    Otherwise treat the string as the vacancy text itself."""
    if path_or_text.startswith(("http://", "https://")):
        path_or_text = _download_to_tmp(path_or_text)
    if not os.path.exists(path_or_text):
        if len(path_or_text) > MAX_INPUT_CHARS:
            print(f"Warning: input text truncated to {MAX_INPUT_CHARS} characters.", file=sys.stderr)
            return path_or_text[:MAX_INPUT_CHARS]
        return path_or_text
    ext = os.path.splitext(path_or_text)[1].lower()
    if ext == ".pdf":
        return _read_pdf(path_or_text)
    if ext in (".docx", ".doc"):
        return _read_docx(path_or_text)
    with open(path_or_text, encoding="utf-8") as f:
        text = f.read()
    if len(text) > MAX_INPUT_CHARS:
        print(f"Warning: file text truncated to {MAX_INPUT_CHARS} characters.", file=sys.stderr)
        text = text[:MAX_INPUT_CHARS]
    return text


# ── Prompt builder ───────────────────────────────────────────────────────────

def build_prompt(resume: str, vacancy: str) -> str:
    return f"""
You are a career advisor helping a job candidate evaluate whether they are a good fit
for a specific vacancy, and how much salary they should ask for.

=== MY RESUME ===
{resume}

=== JOB DESCRIPTION ===
{vacancy}

=== TASK ===
1. Evaluate how well my resume matches this job description.
2. Identify gaps I should be aware of.
3. Suggest what salary range I should request (in USD/month and RUB/month),
   based on the role level, market rates, my experience, and the job location/remote policy.
4. Give me actionable advice: what to emphasize on the interview.
5. Write a ready-to-send reply to the HR recruiter in Russian — a short professional
   message where I express interest, briefly highlight why I'm a good fit, and ask
   a clarifying question. The tone should be friendly but confident.

=== RESPONSE FORMAT ===
Reply with ONLY a valid JSON object. No markdown fences, no preamble, no explanation outside the JSON.

{{
  "match_score": <integer 0-100>,
  "verdict": "<STRONG_FIT | GOOD_FIT | PARTIAL_FIT | NOT_FIT>",
  "summary_ru": "<2-3 предложения на русском — подхожу ли я на эту позицию и почему>",
  "gaps": ["<пробел или риск 1>", "<пробел 2>", "..."],
  "salary_recommendation": {{
    "usd_min": <int, lower bound USD/month>,
    "usd_max": <int, upper bound USD/month>,
    "rub_min": <int, lower bound RUB/month>,
    "rub_max": <int, upper bound RUB/month>,
    "rationale": "<1-2 предложения обоснования диапазона>"
  }},
  "advice_ru": "<Рекомендации на русском: на что делать акцент на собеседовании, к чему готовиться>",
  "hr_reply_ru": "<Готовый ответ HR-рекрутеру на русском — профессиональное сообщение с интересом к позиции>",
  "interview_questions": ["<вопрос к которому готовиться 1>", "<вопрос 2>", "<вопрос 3>"]
}}
""".strip()


# ── Response parsing ─────────────────────────────────────────────────────────

def parse_response(raw: str) -> dict:
    """Extract and parse the JSON block from Claude's output."""
    clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned invalid JSON.\nRaw output:\n{raw}\nError: {e}")

    assert isinstance(data.get("match_score"), int), "match_score must be int"
    assert data.get("verdict") in {
        "STRONG_FIT", "GOOD_FIT", "PARTIAL_FIT", "NOT_FIT",
    }, "Unknown verdict value"
    return data


# ── Report formatter ─────────────────────────────────────────────────────────

def format_report(data: dict) -> str:
    W = 60
    sep = "=" * W
    thin = "-" * W

    lines = [
        sep,
        "  CANDIDATE FIT REPORT".center(W),
        sep,
        f"Score  : {data['match_score']} / 100   [{data['verdict']}]",
        thin,
        "ИТОГ",
        textwrap.indent(textwrap.fill(data["summary_ru"], width=W - 2), "  "),
        thin,
        "ПРОБЕЛЫ / РИСКИ",
    ]
    for g in data.get("gaps", []):
        lines.append(f"  - {g}")

    sal = data.get("salary_recommendation", {})
    lines += [
        thin,
        "РЕКОМЕНДАЦИЯ ПО ЗАРПЛАТЕ",
        f"  USD : {sal.get('usd_min', '?'):,} – {sal.get('usd_max', '?'):,} $/month",
        f"  RUB : {sal.get('rub_min', '?'):,} – {sal.get('rub_max', '?'):,} ₽/month",
        f"  {sal.get('rationale', '')}",
        thin,
        "СОВЕТЫ К СОБЕСЕДОВАНИЮ",
        textwrap.indent(textwrap.fill(data.get("advice_ru", ""), width=W - 2), "  "),
        thin,
        "ОТВЕТ ДЛЯ HR",
        textwrap.indent(textwrap.fill(data.get("hr_reply_ru", ""), width=W - 2), "  "),
        thin,
        "ВОПРОСЫ К ПОДГОТОВКЕ",
    ]
    for i, q in enumerate(data.get("interview_questions", []), 1):
        lines.append(f"  {i}. {q}")
    lines.append(sep)
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

USAGE_TEXT = f"""
HR Resume Matcher — оценка соответствия резюме вакансии через Claude.

Использование:
  python hr_match.py --vacancy <вакансия>              # резюме по умолчанию
  python hr_match.py resume.pdf --vacancy job.pdf      # свое резюме + вакансия PDF
  python hr_match.py resume.pdf --vacancy job.docx     # вакансия в DOCX
  python hr_match.py --vacancy "Senior DevOps..."      # вакансия текстом
  python hr_match.py --vacancy https://example.com/job.pdf  # вакансия по URL

Сохранить отчёт в файл:
  python hr_match.py --vacancy job.pdf --out report.txt

Аргументы:
  resume           Путь, URL к PDF-резюме (необязательно).
                   По умолчанию: {DEFAULT_RESUME_URL}
  --vacancy TEXT   Вакансия: путь к PDF/DOCX/TXT, URL или текст (обязательно).
  --out FILE       Сохранить отчёт в файл.

Отчёт включает:
  - Оценку совпадения (0-100) и вердикт
  - Пробелы и риски
  - Рекомендацию по зарплате (USD и RUB)
  - Советы к собеседованию
  - Готовый ответ для HR-рекрутера
  - Вопросы для подготовки

Требования:
  - Python 3.11+, PyPDF2, python-docx
  - Claude CLI в PATH (claude --print)
""".strip()


def main():
    if len(sys.argv) == 1:
        print(USAGE_TEXT)
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description="HR Resume Matcher via local Claude",
        add_help=True,
    )
    parser.add_argument("resume", nargs="?", default=DEFAULT_RESUME_URL,
                        help="Path or URL to candidate resume (PDF). "
                             "Default: %(default)s")
    parser.add_argument(
        "--vacancy", required=True,
        help="Job description: path/URL to PDF/DOCX/TXT or raw text string",
    )
    parser.add_argument("--out", default=None, help="Optional: save report to this file")
    args = parser.parse_args()

    print("Parsing files...", file=sys.stderr)
    resume_path = args.resume
    if resume_path.startswith(("http://", "https://")):
        resume_path = _download_to_tmp(resume_path)
    resume_text = _read_pdf(resume_path)
    vacancy_text = parse_source(args.vacancy)

    print("Sending to Claude (this may take up to 60 seconds)...", file=sys.stderr)
    prompt = build_prompt(resume_text, vacancy_text)
    raw = ask_claude(prompt)

    print("Parsing response...", file=sys.stderr)
    data = parse_response(raw)

    report = format_report(data)
    print(report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\nReport saved to: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
