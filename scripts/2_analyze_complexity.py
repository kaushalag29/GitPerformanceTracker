#!/usr/bin/env python3
"""
Script 2: Analyze PR complexity and quality using LLM
- Uses OpenRouter free-tier deepseek-v4-flash:free
- Outputs complexity (1-5) and quality (1-5) scores
- Checkpoint-aware: skips already-analyzed PRs
- Account quota: 1000 req/day with $10 credit (7000 PRs in 7 days)

Usage:
    python 2_analyze_complexity.py              # Full analysis (all PRs, 2 threads)
    python 2_analyze_complexity.py --test       # Test mode: 1-2 PRs per author
    python 2_analyze_complexity.py --limit 5    # Custom limit: 5 PRs per author
    python 2_analyze_complexity.py --threads 3  # Use 3 threads (default: 2)
"""

import os
import sys
import csv
import json
import time
import signal
import threading
from pathlib import Path
from typing import Set, Tuple, Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed, FIRST_COMPLETED, wait

import pandas as pd
from dotenv import load_dotenv
from openrouter import OpenRouter

load_dotenv()

# Parse command-line arguments
TEST_MODE = "--test" in sys.argv
LIMIT_PER_AUTHOR = None
THREADS = 2

if "--limit" in sys.argv:
    try:
        idx = sys.argv.index("--limit")
        LIMIT_PER_AUTHOR = int(sys.argv[idx + 1])
    except (IndexError, ValueError):
        print("ERROR: --limit requires a number (e.g., --limit 5)")
        sys.exit(1)

if "--threads" in sys.argv:
    try:
        idx = sys.argv.index("--threads")
        THREADS = int(sys.argv[idx + 1])
    except (IndexError, ValueError):
        print("ERROR: --threads requires a number (e.g., --threads 3)")
        sys.exit(1)

if TEST_MODE:
    LIMIT_PER_AUTHOR = 2
    print("[TEST MODE] Will analyze 2 PRs per author for validation")
elif LIMIT_PER_AUTHOR:
    print(f"[LIMIT MODE] Will analyze {LIMIT_PER_AUTHOR} PRs per author")

print(f"[CONFIG] Using {THREADS} threads (OpenRouter: 1000 req/day with $10 credit, 20 req/min rate limit)")
print(f"[INFO] Account-level quota: 1000 req/day — can process 7000 PRs in 7 days")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    print("ERROR: OPENROUTER_API_KEY not set in .env")
    sys.exit(1)

DATA_DIR = Path(__file__).parent.parent / "data"
ANALYSIS_CSV = DATA_DIR / "pr_analysis.csv"

# Model selection: using high-quality free model
# Account quota: 1000 req/day with $10 credit (enough for 7000 PRs in 7 days)
PRIMARY_MODEL = "deepseek/deepseek-v4-flash:free"

# Thread safety and rate limiting
analysis_lock = threading.Lock()
log_lock = threading.Lock()  # Prevent interleaved log output
shutdown_event = threading.Event()

# Rate limiter: 20 req/min = 1 req every 3 seconds
rate_limit_lock = threading.Lock()
last_request_time = [0]  # Mutable container for closure

def log_print(msg: str):
    """Thread-safe print to prevent interleaved logs."""
    with log_lock:
        print(msg)

def rate_limit_sleep():
    """Enforce 20 req/min rate limit (1 request every 3 seconds)."""
    with rate_limit_lock:
        elapsed = time.time() - last_request_time[0]
        min_interval = 3.0  # 20 req/min = 1 req per 3 seconds
        if elapsed < min_interval:
            sleep_time = min_interval - elapsed
            time.sleep(sleep_time)
        last_request_time[0] = time.time()

def load_checkpoint(csv_path: Path, key_col: str = 'pr_number') -> Set[int]:
    """Load set of already-analyzed PR numbers."""
    if csv_path.exists() and csv_path.stat().st_size > 0:
        try:
            df = pd.read_csv(csv_path)
            return set(df[key_col].astype(int).unique())
        except Exception as e:
            print(f"[WARN] Could not load checkpoint from {csv_path}: {e}")
            return set()
    return set()

def ensure_csv_headers():
    """Create analysis CSV with headers if it doesn't exist."""
    if not ANALYSIS_CSV.exists():
        with open(ANALYSIS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'pr_number', 'complexity_score', 'complexity_rationale',
                'quality_score', 'quality_rationale', 'model_used', 'analyzed_at'
            ])
            writer.writeheader()

def build_review_context(pr_number: int, reviews_df: pd.DataFrame,
                        review_comments_df: pd.DataFrame, comments_df: pd.DataFrame) -> str:
    """Build a rich context from reviews and comments for the LLM prompt."""
    lines = []

    # Fetch reviews for this PR
    reviews = reviews_df[reviews_df['pr_number'] == pr_number]
    for _, r in reviews.iterrows():
        state = r.get('review_state', 'COMMENTED')
        body = r.get('review_body', '')
        if body:
            lines.append(f"[REVIEW by @{r['reviewer']} — {state}]\n{body}")

    # Fetch inline comments for this PR
    inline = review_comments_df[review_comments_df['pr_number'] == pr_number]
    for _, c in inline.iterrows():
        path = c.get('path', 'unknown')
        hunk = c.get('diff_hunk', '')
        body = c.get('comment_body', '')
        if body:
            lines.append(f"[INLINE @{c['reviewer']} on {path}]\n{body}")

    # Fetch discussion comments for this PR
    discussion = comments_df[comments_df['pr_number'] == pr_number]
    for _, c in discussion.iterrows():
        if c['commenter'] != c['pr_author']:  # Skip author's own comments
            body = c.get('comment_body', '')
            if body:
                lines.append(f"[DISCUSSION by @{c['commenter']}]\n{body}")

    return "\n\n".join(lines)  # Full review context, no truncation

def call_openrouter_api(system_prompt: str, user_prompt: str, max_retries: int = 2) -> Tuple[Optional[str], bool, str]:
    """Call OpenRouter API with rate limiting. Returns (response_text, success, model_used)."""
    rate_limit_sleep()  # Enforce 20 req/min
    log_print(f"    [API CALL] Using {PRIMARY_MODEL}...")

    for attempt in range(max_retries):
        try:
            client = OpenRouter(api_key=OPENROUTER_API_KEY)
            response = client.chat.send(
                model=PRIMARY_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                max_tokens=300,
            )
            content = response.choices[0].message.content
            if content:
                log_print(f"    [API SUCCESS] Got response ({len(content)} chars)")
                return content, True, PRIMARY_MODEL
            else:
                log_print(f"    [API ERROR] Response was empty")
                return None, False, PRIMARY_MODEL

        except Exception as e:
            error_msg = str(e)

            # Handle rate limiting (429)
            if "429" in error_msg or "rate limit" in error_msg.lower():
                log_print(f"[WARN] Rate limited (429). Account daily quota may be exhausted. Re-run tomorrow.")
                return None, False, PRIMARY_MODEL

            # Handle quota exhausted
            if "quota" in error_msg.lower():
                log_print(f"[WARN] OpenRouter quota exhausted.")
                return None, False, PRIMARY_MODEL

            # Transient errors: retry with backoff
            log_print(f"[WARN] API call failed (attempt {attempt + 1}/{max_retries}): {str(e)[:150]}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            return None, False, PRIMARY_MODEL

    return None, False, PRIMARY_MODEL

def parse_json_response(content: str) -> Tuple[int, str, int, str]:
    """Parse LLM JSON response. Extract and handle flexible field names."""
    try:
        # Extract first complete JSON object
        start_idx = content.find('{')
        if start_idx == -1:
            log_print(f"    [PARSE ERROR] No JSON object found in: {content[:100]}")
            return 3, 'parse_error', 3, 'parse_error'

        # Find the matching closing brace (handle nested objects)
        brace_count = 0
        end_idx = -1
        for i in range(start_idx, len(content)):
            if content[i] == '{':
                brace_count += 1
            elif content[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i
                    break

        if end_idx == -1:
            log_print(f"    [PARSE ERROR] Unmatched braces in: {content[:100]}")
            return 3, 'parse_error', 3, 'parse_error'

        json_str = content[start_idx:end_idx + 1]
        data = json.loads(json_str)

        # Extract scores with flexible field names
        complexity = int(data.get('complexity_score', data.get('complexity', 3)))
        complexity = max(1, min(5, complexity))

        quality = int(data.get('quality_score', data.get('quality', 3)))
        quality = max(1, min(5, quality))

        # Extract rationales with multiple field name options
        complexity_rat = (
            data.get('complexity_rationale') or
            data.get('complexity_basis') or
            data.get('complexity_explanation') or
            'no explanation provided'
        )
        complexity_rat = str(complexity_rat).strip()

        quality_rat = (
            data.get('quality_rationale') or
            data.get('quality_basis') or
            data.get('quality_explanation') or
            'no explanation provided'
        )
        quality_rat = str(quality_rat).strip()

        # Validate rationales aren't empty/None
        if not complexity_rat or complexity_rat.lower() == 'none':
            complexity_rat = 'no explanation provided'
        if not quality_rat or quality_rat.lower() == 'none':
            quality_rat = 'no explanation provided'

        return complexity, complexity_rat, quality, quality_rat

    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        log_print(f"    [PARSE ERROR] JSON parsing failed: {str(e)[:80]}")
        log_print(f"    [PARSE ERROR] Content (first 200): {content[:200]}")
        return 3, 'parse_error', 3, 'parse_error'

def write_analysis_batch(rows: list):
    """Write analysis rows to CSV under lock with fsync for persistence."""
    if not rows:
        return
    try:
        log_print(f"  [FLUSH] Writing {len(rows)} results to pr_analysis.csv...")
        with analysis_lock:
            with open(ANALYSIS_CSV, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                for row in rows:
                    writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())
        log_print(f"  [FLUSH COMPLETE] {len(rows)} results persisted to disk")
    except Exception as e:
        log_print(f"[ERROR] Failed to write analysis batch: {e}")

def fetch_pr_analysis(pr_number: int, pr_row: dict,
                     reviews_df: pd.DataFrame,
                     review_comments_df: pd.DataFrame,
                     comments_df: pd.DataFrame) -> Optional[dict]:
    """
    Analyze single PR via LLM.
    Returns: dict with analysis data, or None if failed.
    """
    if shutdown_event.is_set():
        return None

    try:
        log_print(f"  [ANALYZE] PR #{pr_number} by @{pr_row['author']} — building context...")

        # Build LLM context
        review_context = build_review_context(pr_number, reviews_df, review_comments_df, comments_df)

        # Build prompts - MUST be JSON-only output
        system_prompt = """CRITICAL INSTRUCTION: Output ONLY a valid JSON object. No other text.

If you understand, output only the JSON below with scores 1-5.
Do not include:
- thinking or reasoning
- explanations before/after
- markdown formatting
- any text outside the JSON

The JSON format is:
{"complexity_score": 1-5, "complexity_rationale": "sentence", "quality_score": 1-5, "quality_rationale": "sentence"}"""

        user_prompt = f"""EVALUATE THIS GITHUB PR:

Title: {pr_row['title']}
Author: @{pr_row['author']}
Reverted: {pr_row.get('was_reverted', False)}
Changes: {int(pr_row['additions'])} added, {int(pr_row['deletions'])} removed, {int(pr_row['changed_files'])} files

Description: {pr_row['body'] or "None"}

Code diff: {pr_row.get('pr_patch_truncated', 'None')}

Review feedback: {review_context or "None"}

SCORE THIS PR:
- Complexity (1=trivial docs, 2=single fix, 3=multi-file feature, 4=architectural, 5=major feature)
- Quality (1=reverted/broken, 2=many rework cycles, 3=2-3 revisions, 4=clean, 5=first-pass perfect)

OUTPUT ONLY THIS JSON (nothing else):
{{"complexity_score": <1-5>, "complexity_rationale": "<one sentence>", "quality_score": <1-5>, "quality_rationale": "<one sentence>"}}"""

        # Call OpenRouter API (includes rate limiting and model rotation)
        content, success, model_used = call_openrouter_api(system_prompt, user_prompt)

        if not success or content is None:
            log_print(f"  [ANALYZE FAILED] PR #{pr_number} — API call failed")
            return None

        # Parse response
        complexity, complexity_rat, quality, quality_rat = parse_json_response(content)

        # Validate scores
        complexity = max(1, min(5, complexity))
        quality = max(1, min(5, quality))

        log_print(f"  [ANALYZE COMPLETE] PR #{pr_number} — complexity={complexity}, quality={quality}")

        return {
            'pr_number': pr_number,
            'complexity_score': complexity,
            'complexity_rationale': complexity_rat,
            'quality_score': quality,
            'quality_rationale': quality_rat,
            'model_used': model_used,
            'analyzed_at': pd.Timestamp.now().isoformat(),
        }

    except Exception as e:
        log_print(f"[ERROR] Failed to analyze PR #{pr_number}: {e}")
        return None

def analyze_prs():
    """Analyze all unprocessed PRs for complexity and quality using multithreading."""
    print(f"[STEP 1] Analyzing PR complexity and quality ({THREADS} threads, 20 req/min rate limit)...")

    ensure_csv_headers()

    # Load data
    prs_df = pd.read_csv(DATA_DIR / "prs.csv")
    reviews_df = pd.read_csv(DATA_DIR / "pr_reviews.csv") if (DATA_DIR / "pr_reviews.csv").exists() else pd.DataFrame()
    review_comments_df = pd.read_csv(DATA_DIR / "pr_review_comments.csv") if (DATA_DIR / "pr_review_comments.csv").exists() else pd.DataFrame()
    comments_df = pd.read_csv(DATA_DIR / "pr_comments.csv") if (DATA_DIR / "pr_comments.csv").exists() else pd.DataFrame()

    # Load checkpoint
    processed = load_checkpoint(ANALYSIS_CSV)
    print(f"[INFO] Already analyzed {len(processed)} PRs, {len(prs_df) - len(processed)} to go")

    # Build work queue
    work_queue = []
    authors_analyzed = {}

    for _, pr_row in prs_df.iterrows():
        pr_number = int(pr_row['pr_number'])
        author = pr_row['author']

        # Skip if already analyzed
        if pr_number in processed:
            continue

        # Skip if hit per-author limit (for test/limit modes)
        if LIMIT_PER_AUTHOR:
            author_count = authors_analyzed.get(author, 0)
            if author_count >= LIMIT_PER_AUTHOR:
                continue
            authors_analyzed[author] = author_count + 1

        work_queue.append((pr_number, pr_row))

    if not work_queue:
        print("[OK] All PRs already analyzed")
        return

    print(f"[INFO] Queued {len(work_queue)} PRs for analysis")

    analyzed_count = [0]
    failed_count = [0]
    parse_error_prs = []  # Track PRs that failed to parse for retry
    futures = set()  # Use set for as_completed()
    future_to_pr = {}  # Separate mapping to avoid modifying dict during iteration
    interrupt_count = [0]
    batch_size = 10  # Submit 10 PRs at a time

    def signal_handler(signum, frame):
        interrupt_count[0] += 1
        if interrupt_count[0] == 1:
            print(f"\n[INFO] Received interrupt. Flushing and exiting... (Ctrl+C again to force-kill)")
            shutdown_event.set()
        else:
            print(f"\n[FATAL] Force-exiting immediately...")
            import os
            os._exit(1)

    signal.signal(signal.SIGINT, signal_handler)

    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        submitted_count = 0
        result_batch = []
        completed = 0

        # Submit first batch of 10
        log_print(f"\n[BATCH 1] Submitting first {min(batch_size, len(work_queue))} PRs...")
        for i in range(min(batch_size, len(work_queue))):
            pr_number, pr_row = work_queue[i]
            future = executor.submit(
                fetch_pr_analysis, pr_number, pr_row,
                reviews_df, review_comments_df, comments_df
            )
            futures.add(future)
            future_to_pr[future] = pr_number
            submitted_count += 1
            log_print(f"  [SUBMIT] PR #{pr_number} by @{pr_row['author']} queued")
        log_print(f"[BATCH 1 COMPLETE] Submitted {submitted_count}/{len(work_queue)} PRs, waiting for results...\n")

        # Process completions with wait() to handle dynamic futures properly
        while futures or submitted_count < len(work_queue):
            # Check if shutdown requested
            if shutdown_event.is_set():
                log_print(f"[INFO] Shutdown requested. Stopping.")
                break

            # Submit more if we have room
            queue_size = len(futures)
            while submitted_count < len(work_queue) and queue_size < batch_size * 2:
                if shutdown_event.is_set():
                    break
                pr_number, pr_row = work_queue[submitted_count]
                future = executor.submit(
                    fetch_pr_analysis, pr_number, pr_row,
                    reviews_df, review_comments_df, comments_df
                )
                futures.add(future)
                future_to_pr[future] = pr_number
                submitted_count += 1
                queue_size += 1
                if submitted_count % batch_size == 0:
                    log_print(f"\n[BATCH {submitted_count // batch_size + 1}] Submitted {submitted_count}/{len(work_queue)} PRs total, {queue_size} in-flight\n")

            # Wait for at least one future to complete (with timeout)
            if not futures:
                break

            done, pending = wait(futures, timeout=5, return_when=FIRST_COMPLETED)

            # Process completed futures
            for future in done:
                futures.discard(future)  # Remove from active set
                pr_number = future_to_pr.get(future)

                # Get pr_row from work_queue
                pr_row = None
                for pn, row in work_queue:
                    if pn == pr_number:
                        pr_row = row
                        break

                try:
                    result = future.result(timeout=5)
                    if result is not None:
                        # Track parse errors for retry
                        if result.get('complexity_rationale') == 'parse_error' or result.get('quality_rationale') == 'parse_error':
                            if pr_row is not None:
                                parse_error_prs.append((pr_number, pr_row))
                                log_print(f"[PARSE ERROR] PR #{pr_number} — will retry later")
                            else:
                                log_print(f"[PARSE ERROR] PR #{pr_number} — couldn't find row for retry")

                        # Ensure rationales are always strings (never numbers)
                        result['complexity_rationale'] = str(result['complexity_rationale']).strip()
                        result['quality_rationale'] = str(result['quality_rationale']).strip()

                        result_batch.append(result)
                        analyzed_count[0] += 1
                        log_print(f"[RESULT] PR #{pr_number} analyzed (batch: {len(result_batch)}/10)")
                    else:
                        failed_count[0] += 1
                        log_print(f"[RESULT SKIP] PR #{pr_number} returned None")

                    # Flush result batch every 10 analyses
                    if len(result_batch) >= 10:
                        write_analysis_batch(result_batch)
                        result_batch = []

                    completed += 1
                    if completed % 50 == 0:
                        log_print(f"\n[PROGRESS] Completed {completed}/{len(work_queue)} — {analyzed_count[0]} analyzed, {failed_count[0]} failed, {len(futures)} in-flight\n")

                except Exception as e:
                    log_print(f"[ERROR] PR #{pr_number}: {e}")
                    failed_count[0] += 1

        # Flush remaining result batch before shutdown
        if result_batch:
            log_print(f"[INFO] Flushing final batch ({len(result_batch)} PRs)...")
            write_analysis_batch(result_batch)

        # Retry PRs that failed to parse
        log_print(f"\n[RETRY PHASE] Found {len(parse_error_prs)} PRs with parse errors")
        if parse_error_prs and not shutdown_event.is_set():
            log_print(f"[RETRY] Starting retry of {len(parse_error_prs)} PRs with parse errors...\n")
            retry_batch = []
            for pr_number, pr_row in parse_error_prs:
                try:
                    review_context = build_review_context(pr_number, reviews_df, review_comments_df, comments_df)
                    system_prompt = """CRITICAL INSTRUCTION: Output ONLY a valid JSON object. No other text.

If you understand, output only the JSON below with scores 1-5.
Do not include:
- thinking or reasoning
- explanations before/after
- markdown formatting
- any text outside the JSON

The JSON format is:
{"complexity_score": 1-5, "complexity_rationale": "sentence", "quality_score": 1-5, "quality_rationale": "sentence"}"""

                    user_prompt = f"""EVALUATE THIS GITHUB PR:

Title: {pr_row['title']}
Author: @{pr_row['author']}
Reverted: {pr_row.get('was_reverted', False)}
Changes: {int(pr_row['additions'])} added, {int(pr_row['deletions'])} removed, {int(pr_row['changed_files'])} files

Description: {pr_row['body'] or "None"}

Code diff: {pr_row.get('pr_patch_truncated', 'None')}

Review feedback: {review_context or "None"}

SCORE THIS PR:
- Complexity (1=trivial docs, 2=single fix, 3=multi-file feature, 4=architectural, 5=major feature)
- Quality (1=reverted/broken, 2=many rework cycles, 3=2-3 revisions, 4=clean, 5=first-pass perfect)

OUTPUT ONLY THIS JSON (nothing else):
{{"complexity_score": <1-5>, "complexity_rationale": "<one sentence>", "quality_score": <1-5>, "quality_rationale": "<one sentence>"}}"""

                    log_print(f"  [RETRY] PR #{pr_number} — calling API...")
                    content, success, model_used = call_openrouter_api(system_prompt, user_prompt)

                    if success and content:
                        complexity, complexity_rat, quality, quality_rat = parse_json_response(content)
                        complexity = max(1, min(5, complexity))
                        quality = max(1, min(5, quality))

                        retry_result = {
                            'pr_number': pr_number,
                            'complexity_score': complexity,
                            'complexity_rationale': str(complexity_rat).strip(),  # Ensure string
                            'quality_score': quality,
                            'quality_rationale': str(quality_rat).strip(),  # Ensure string
                            'model_used': model_used,
                            'analyzed_at': pd.Timestamp.now().isoformat(),
                        }
                        retry_batch.append(retry_result)
                        log_print(f"  [RETRY SUCCESS] PR #{pr_number} — complexity={complexity}, quality={quality} (via {model_used})")
                    else:
                        log_print(f"  [RETRY FAILED] PR #{pr_number} — API call failed again")

                except Exception as e:
                    log_print(f"  [RETRY ERROR] PR #{pr_number}: {e}")

            # Flush retry batch
            if retry_batch:
                log_print(f"\n[RETRY FLUSH] Writing {len(retry_batch)} retried results...")
                write_analysis_batch(retry_batch)

        # Log exit reason
        log_print(f"\n[DEBUG] as_completed loop finished. Submitted: {submitted_count}/{len(work_queue)}")
        log_print(f"[DEBUG] Completed: {completed}, Total futures: {len(futures)}")

        # Cancel all remaining futures
        remaining = sum(1 for f in futures if not f.done())
        if remaining > 0:
            log_print(f"[INFO] Cancelling {remaining} unprocessed futures...")
            cancelled = 0
            for future in futures:
                if future.cancel():
                    cancelled += 1
            if cancelled > 0:
                log_print(f"[INFO] Cancelled {cancelled} futures")

        # Shutdown executor without waiting
        executor.shutdown(wait=False)

    print(f"[OK] Analyzed {analyzed_count[0]} new PRs ({failed_count[0]} failed)")

    # Show per-author breakdown in test/limit mode
    if LIMIT_PER_AUTHOR and authors_analyzed:
        unique_authors = len(authors_analyzed)
        print(f"[INFO] Covered {unique_authors} unique authors")
        for author, count in sorted(authors_analyzed.items(), key=lambda x: x[1], reverse=True):
            print(f"       @{author}: {count} PR{'s' if count != 1 else ''}")


if __name__ == '__main__':
    try:
        analyze_prs()
        print("\n✅ Analysis complete!")
        print(f"   pr_analysis.csv: {ANALYSIS_CSV}")
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted. Checkpoint saved. Re-run to resume.")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)
