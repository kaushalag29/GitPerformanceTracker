#!/usr/bin/env python3
"""
Script 1: Collect GitHub PR data from PostHog/posthog
- Fetches merged PRs from last 90 days
- Collects PR metadata, reviews, comments (multithreaded)
- Checkpoint-aware: skips already-processed PRs
- Rate-limit aware: sleeps when needed

Usage:
    python 1_collect_data.py              # Full collection (2 threads, throttled)
    python 1_collect_data.py --test       # Test mode: 3 PRs, 2 threads
    python 1_collect_data.py --limit 10   # 10 PRs, 2 threads
    python 1_collect_data.py --threads 4  # Full collection, 4 threads (custom)
"""

import os
import sys
import csv
import time
import re
import threading
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Set, Tuple

import pandas as pd
from github import Github, GithubException, Auth

# Load environment
from dotenv import load_dotenv
load_dotenv()

# Global flag for graceful shutdown
shutdown_event = threading.Event()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    print("ERROR: GITHUB_TOKEN not set in .env")
    sys.exit(1)

# Parse command-line arguments
TEST_MODE = "--test" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    try:
        idx = sys.argv.index("--limit")
        LIMIT = int(sys.argv[idx + 1])
    except (IndexError, ValueError):
        print("ERROR: --limit requires a number (e.g., --limit 10)")
        sys.exit(1)

if TEST_MODE:
    LIMIT = 3
    print("[TEST MODE] Will fetch only 3 PRs for testing")
elif LIMIT:
    print(f"[LIMIT MODE] Will fetch only {LIMIT} PRs")

# Parse thread count
THREADS = 2
if "--threads" in sys.argv:
    try:
        idx = sys.argv.index("--threads")
        THREADS = int(sys.argv[idx + 1])
    except (IndexError, ValueError):
        print("ERROR: --threads requires a number (e.g., --threads 10)")
        sys.exit(1)
print(f"[INFO] Using {THREADS} threads for review collection")

# Initialize GitHub client
g = Github(auth=Auth.Token(GITHUB_TOKEN))
repo = g.get_repo("PostHog/posthog")

# Setup data directory
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PRS_CSV = DATA_DIR / "prs.csv"
REVIEWS_CSV = DATA_DIR / "pr_reviews.csv"
REVIEW_COMMENTS_CSV = DATA_DIR / "pr_review_comments.csv"
COMMENTS_CSV = DATA_DIR / "pr_comments.csv"

# Thread-safe CSV write locks
reviews_lock = threading.Lock()
review_comments_lock = threading.Lock()
comments_lock = threading.Lock()

# Signal handler for graceful shutdown on Ctrl+C
signal_count = [0]  # Use list to allow modification in nested function

def signal_handler(signum, frame):
    signal_count[0] += 1
    if signal_count[0] == 1:
        print("\n[INFO] Received interrupt signal. Workers will complete and flush writes...")
        shutdown_event.set()
        # Don't call sys.exit() - let the program finish naturally
    else:
        print(f"\n[FATAL] Force killing (Ctrl+C pressed {signal_count[0]} times)...")
        import os
        os._exit(1)  # Use os._exit to force kill without cleanup

signal.signal(signal.SIGINT, signal_handler)

# Calculate cutoff date (90 days ago)
cutoff_date = datetime.now(timezone.utc) - timedelta(days=90)
today = datetime.now(timezone.utc).date()
print(f"[INFO] Today: {today}")
print(f"[INFO] Cutoff date (90 days ago): {cutoff_date.date()}")
print(f"[INFO] Will collect merged PRs from {cutoff_date.date()} to {today}")

def load_checkpoint(csv_path: Path, key_col: str) -> Set[int]:
    """Load set of already-processed PR numbers from CSV."""
    if csv_path.exists() and csv_path.stat().st_size > 0:
        try:
            df = pd.read_csv(csv_path)
            return set(df[key_col].unique())
        except Exception as e:
            print(f"[WARN] Could not load checkpoint from {csv_path}: {e}")
            return set()
    return set()

def ensure_csv_headers():
    """Create CSV files with headers if they don't exist."""
    # prs.csv
    if not PRS_CSV.exists():
        with open(PRS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'pr_number', 'title', 'body', 'author', 'created_at', 'merged_at',
                'turnaround_hours', 'additions', 'deletions', 'changed_files', 'commits',
                'review_comment_count', 'general_comment_count', 'is_revert', 'was_reverted',
                'labels', 'pr_patch_truncated'
            ])
            writer.writeheader()

    # pr_reviews.csv
    if not REVIEWS_CSV.exists():
        with open(REVIEWS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'pr_number', 'pr_author', 'reviewer', 'review_state', 'review_body', 'submitted_at'
            ])
            writer.writeheader()

    # pr_review_comments.csv
    if not REVIEW_COMMENTS_CSV.exists():
        with open(REVIEW_COMMENTS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'pr_number', 'reviewer', 'path', 'diff_hunk', 'comment_body'
            ])
            writer.writeheader()

    # pr_comments.csv
    if not COMMENTS_CSV.exists():
        with open(COMMENTS_CSV, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'pr_number', 'pr_author', 'commenter', 'comment_body', 'created_at'
            ])
            writer.writeheader()

# Linked issue parsing removed - PR body contains all context needed

def get_pr_patch(pr) -> str:
    """Concatenate diff patches from all files, truncated."""
    try:
        patches = []
        for file in pr.get_files():
            if file.patch:
                patches.append(file.patch)
        return "\n".join(patches)[:4000]
    except Exception as e:
        print(f"[WARN] Could not get patch for PR #{pr.number}: {e}")
        return ""

def write_to_csv(csv_path: Path, rows: list, lock: threading.Lock):
    """Write rows to CSV with automatic flushing."""
    if not rows:
        return
    try:
        with lock:
            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                for row in rows:
                    writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())  # Force OS to write to disk
    except Exception as e:
        print(f"[ERROR] Failed to write to {csv_path}: {e}")

def fetch_pr_review_data(pr_number: int, pr_author: str, processed_reviews: Set[int],
                         processed_review_comments: Set[int], processed_comments: Set[int]) -> Tuple[int, int, int]:
    """Fetch reviews, inline comments, and discussion comments for a single PR (thread worker).

    Workers continue to completion even after shutdown signal—no early exit.
    """
    try:
        # Use shared global repo object instead of creating new client per thread
        pr = repo.get_pull(pr_number)
        time.sleep(1.0)  # Throttle requests to avoid secondary rate limits

        review_count = 0
        review_comment_count = 0
        discussion_comment_count = 0

        # Fetch reviews
        if pr_number not in processed_reviews:
            reviews = []
            for review in pr.get_reviews():
                reviews.append({
                    'pr_number': pr_number,
                    'pr_author': pr_author,
                    'reviewer': review.user.login,
                    'review_state': review.state,
                    'review_body': review.body or '',
                    'submitted_at': review.submitted_at.isoformat() if review.submitted_at else '',
                })

            if reviews:
                write_to_csv(REVIEWS_CSV, reviews, reviews_lock)
                review_count = len(reviews)
            else:
                # Write placeholder row for PRs with no reviews (marks as processed)
                write_to_csv(REVIEWS_CSV, [{'pr_number': pr_number, 'pr_author': pr_author, 'reviewer': '', 'review_state': '', 'review_body': '', 'submitted_at': ''}], reviews_lock)

        # Fetch inline review comments
        if pr_number not in processed_review_comments:
            inline_comments = []
            for comment in pr.get_review_comments():
                inline_comments.append({
                    'pr_number': pr_number,
                    'reviewer': comment.user.login,
                    'path': comment.path,
                    'diff_hunk': comment.diff_hunk or '',
                    'comment_body': comment.body or '',
                })

            if inline_comments:
                write_to_csv(REVIEW_COMMENTS_CSV, inline_comments, review_comments_lock)
                review_comment_count = len(inline_comments)
            else:
                # Write placeholder row for PRs with no inline comments
                write_to_csv(REVIEW_COMMENTS_CSV, [{'pr_number': pr_number, 'reviewer': '', 'path': '', 'diff_hunk': '', 'comment_body': ''}], review_comments_lock)

        # Fetch discussion comments
        if pr_number not in processed_comments:
            discussion_comments = []
            for comment in pr.get_issue_comments():
                discussion_comments.append({
                    'pr_number': pr_number,
                    'pr_author': pr_author,
                    'commenter': comment.user.login,
                    'comment_body': comment.body or '',
                    'created_at': comment.created_at.isoformat(),
                })

            if discussion_comments:
                write_to_csv(COMMENTS_CSV, discussion_comments, comments_lock)
                discussion_comment_count = len(discussion_comments)
            else:
                # Write placeholder row for PRs with no discussion comments
                write_to_csv(COMMENTS_CSV, [{'pr_number': pr_number, 'pr_author': pr_author, 'commenter': '', 'comment_body': '', 'created_at': ''}], comments_lock)

        return review_count, review_comment_count, discussion_comment_count

    except Exception as e:
        print(f"[WARN] Failed to fetch review data for PR #{pr_number}: {e}")
        return 0, 0, 0

def collect_prs():
    """Fetch and store all merged PRs from last 90 days."""
    print("\n[STEP 1] Collecting PR metadata...")

    ensure_csv_headers()

    # Load checkpoint
    processed_prs = load_checkpoint(PRS_CSV, 'pr_number')
    print(f"[INFO] Checkpoint loaded: {len(processed_prs)} PRs already in prs.csv")

    max_pr_num = None
    if processed_prs:
        min_pr = min(processed_prs)
        max_pr = max(processed_prs)
        max_pr_num = max_pr
        print(f"       PR numbers range: #{min_pr} to #{max_pr}")
        print(f"[INFO] Will only fetch NEW PRs (PR number > #{max_pr_num})")

    # Fetch all closed PRs (sorted by creation date, descending)
    pr_count = 0
    skipped_count = 0
    consecutive_skipped = 0  # Track consecutive already-processed PRs
    pr_check_count = 0  # Total PRs checked (including skipped)

    print(f"[INFO] Starting PR iteration (fetching only NEW PRs)...")
    for pr in repo.get_pulls(state='closed', sort='created', direction='desc'):
        pr_check_count += 1

        # Skip old PRs if we have a checkpoint - this DRAMATICALLY speeds up re-runs
        if max_pr_num and pr.number <= max_pr_num:
            print(f"[INFO] Reached PR #{pr.number} (already in checkpoint). Stopping - no new PRs to fetch!")
            break

        # Log progress every 10 new PRs checked
        if pr_check_count % 10 == 0:
            print(f"[INFO] Found new PR #{pr.number}... (checked {pr_check_count}, collected {pr_count})")

        # Check if we've hit the limit (for testing)
        if LIMIT and pr_count >= LIMIT:
            print(f"[INFO] Reached limit of {LIMIT} PRs. Stopping collection.")
            break

        # Check rate limit every 50 NEW PRs
        if pr_count % 50 == 0 and pr_count > 0:
            try:
                rate_limit = g.get_rate_limit()
                remaining = rate_limit.resources.core.remaining
                print(f"[INFO] Processed {pr_count} PRs, {remaining} API calls remaining")
                if remaining < 200:
                    reset_time = rate_limit.resources.core.reset
                    sleep_sec = (reset_time - datetime.now(timezone.utc)).total_seconds() + 10
                    print(f"[WARN] Rate limit low. Sleeping {sleep_sec:.0f}s...")
                    time.sleep(max(sleep_sec, 0))
            except (AttributeError, Exception) as e:
                # If rate limit check fails, just log and continue
                print(f"[WARN] Could not check rate limit: {e}. Continuing...")
                time.sleep(1)  # Small delay to be safe

        # Skip if not merged
        if pr.merged_at is None:
            continue

        # Skip if before cutoff date
        if pr.merged_at.replace(tzinfo=None) < cutoff_date.replace(tzinfo=None):
            print(f"[INFO] Hit cutoff date. Stopping collection.")
            break

        # Skip if already processed
        if pr.number in processed_prs:
            skipped_count += 1
            consecutive_skipped += 1
            # Should rarely happen now since we skip by max_pr_num
            if consecutive_skipped > 10:
                print(f"[INFO] Hit {consecutive_skipped} consecutive already-processed PRs. Stopping early.")
                break
            continue

        consecutive_skipped = 0  # Reset counter when we find a new PR

        # Skip bot PRs
        if pr.user.type == 'Bot' or '[bot]' in pr.user.login:
            continue

        try:
            # Determine if this is a revert
            is_revert = pr.title.lower().startswith('revert') or (
                pr.body and 'this reverts commit' in pr.body.lower()
            )

            # Calculate turnaround
            turnaround = (pr.merged_at - pr.created_at).total_seconds() / 3600.0

            # Get patch
            patch = get_pr_patch(pr)

            # Get labels
            labels = ",".join([l.name for l in pr.labels])

            row = {
                'pr_number': pr.number,
                'title': pr.title,
                'body': pr.body or '',
                'author': pr.user.login,
                'created_at': pr.created_at.isoformat(),
                'merged_at': pr.merged_at.isoformat(),
                'turnaround_hours': turnaround,
                'additions': pr.additions,
                'deletions': pr.deletions,
                'changed_files': pr.changed_files,
                'commits': pr.commits,
                'review_comment_count': pr.review_comments,
                'general_comment_count': pr.comments,
                'is_revert': is_revert,
                'was_reverted': '',  # Will be filled in post-processing
                'labels': labels,
                'pr_patch_truncated': patch,
            }

            # Append to CSV
            with open(PRS_CSV, 'a', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=row.keys())
                writer.writerow(row)

            pr_count += 1
            if pr_count % 50 == 0:
                print(f"  → Collected {pr_count} PRs so far...")

        except Exception as e:
            print(f"[ERROR] Failed to process PR #{pr.number}: {e}")
            continue

    print(f"[OK] Collected {pr_count} new PRs (skipped {skipped_count} already processed)")
    print(f"[INFO] Total PRs in prs.csv now: {len(processed_prs) + pr_count}")
    return pr_count

def collect_all_review_data():
    """Fetch reviews, inline comments, and discussion comments for all PRs (multithreaded)."""
    print(f"\n[STEP 2] Collecting all review data ({THREADS} threads)...")

    # Load checkpoints once
    processed_reviews = load_checkpoint(REVIEWS_CSV, 'pr_number')
    processed_review_comments = load_checkpoint(REVIEW_COMMENTS_CSV, 'pr_number')
    processed_comments = load_checkpoint(COMMENTS_CSV, 'pr_number')

    print(f"[INFO] Checkpoint status:")
    print(f"       pr_reviews.csv: {len(processed_reviews)} PRs already processed")
    print(f"       pr_review_comments.csv: {len(processed_review_comments)} PRs already processed")
    print(f"       pr_comments.csv: {len(processed_comments)} PRs already processed")

    # Load PR data to iterate
    prs_df = pd.read_csv(PRS_CSV)
    print(f"[INFO] prs.csv contains {len(prs_df)} total PRs to process reviews for")

    # Build list of PRs to fetch - ONLY those needing work
    prs_needing_work = []
    for _, r in prs_df.iterrows():
        pr_num = int(r['pr_number'])
        # Only add if missing ANY of the three data types
        if pr_num not in processed_reviews or pr_num not in processed_review_comments or pr_num not in processed_comments:
            prs_needing_work.append((pr_num, r['author']))

    print(f"[INFO] PRs needing review data: {len(prs_needing_work)} out of {len(prs_df)}")

    if len(prs_needing_work) == 0:
        print(f"[OK] All PRs already have complete review data. Nothing to do!")
        return

    review_total = 0
    inline_total = 0
    discussion_total = 0
    processed_count = 0

    # Run threaded collection
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        # Submit only PRs that need work
        futures = {
            executor.submit(fetch_pr_review_data, pr_num, author,
                          processed_reviews, processed_review_comments, processed_comments): pr_num
            for pr_num, author in prs_needing_work
        }

        # Process results as they complete
        for future in as_completed(futures):
            # Break immediately if shutdown signal received
            if shutdown_event.is_set():
                print(f"[INFO] Shutdown signal detected. Stopping work queue...")
                break

            pr_num = futures[future]
            reviews, inline, discussion = future.result()
            review_total += reviews
            inline_total += inline
            discussion_total += discussion
            processed_count += 1

            if processed_count % 50 == 0:
                print(f"  → Processed {processed_count}/{len(prs_needing_work)} PRs needing work...")

        # Cancel all remaining futures
        print(f"[INFO] Cancelling {len(futures)} remaining futures...")
        cancelled = 0
        for future in futures:
            if future.cancel():
                cancelled += 1
        print(f"[INFO] Cancelled {cancelled} futures")

        # Shutdown executor WITHOUT waiting (don't block on stuck API calls)
        executor.shutdown(wait=False)
        print(f"[INFO] Executor shutdown initiated (non-blocking).")

    # Reload checkpoints to get actual counts (accounts for concurrent writes)
    final_reviews = load_checkpoint(REVIEWS_CSV, 'pr_number')
    final_inline = load_checkpoint(REVIEW_COMMENTS_CSV, 'pr_number')
    final_comments = load_checkpoint(COMMENTS_CSV, 'pr_number')

    new_reviews = len(final_reviews) - len(processed_reviews)
    new_inline = len(final_inline) - len(processed_review_comments)
    new_comments = len(final_comments) - len(processed_comments)

    print(f"[OK] New reviews: {max(0, new_reviews)} | New inline: {max(0, new_inline)} | New discussion: {max(0, new_comments)}")

def link_reverts():
    """Identify which PRs were later reverted."""
    print("\n[STEP 3] Linking reverted PRs...")

    prs_df = pd.read_csv(PRS_CSV)

    # Convert pr_number to int, handle NaN
    prs_df['pr_number'] = pd.to_numeric(prs_df['pr_number'], errors='coerce').dropna().astype(int)
    prs_df = prs_df.dropna(subset=['pr_number'])

    # Convert was_reverted to boolean (handles float/NaN values from CSV)
    prs_df['was_reverted'] = prs_df['was_reverted'].fillna('').astype(str).str.lower() == 'true'

    # Find all revert PRs
    revert_prs = prs_df[prs_df['is_revert'] == True]

    # Early exit if no reverts
    if len(revert_prs) == 0:
        print(f"[OK] No revert PRs found. Skipping.")
        return

    revert_count = 0
    for _, revert_row in revert_prs.iterrows():
        try:
            revert_title = revert_row['title']
            revert_body = revert_row['body'] or ''

            # Try to find the original PR number
            original_pr_num = None

            # Try parsing from body (standard GitHub revert format)
            match = re.search(r'Pull Request\s+#(\d+)', revert_body, re.IGNORECASE)
            if match:
                original_pr_num = int(match.group(1))

            # If not found, try to extract from title
            if not original_pr_num:
                match = re.search(r'#(\d+)', revert_title)
                if match:
                    original_pr_num = int(match.group(1))

            # If found, mark the original PR as reverted
            if original_pr_num and original_pr_num in prs_df['pr_number'].values:
                prs_df.loc[prs_df['pr_number'] == original_pr_num, 'was_reverted'] = True
                revert_count += 1
        except Exception as e:
            print(f"[WARN] Failed to process revert PR: {e}")
            continue

    # Only save if changes were made
    if revert_count > 0:
        prs_df.to_csv(PRS_CSV, index=False)
        print(f"[OK] Marked {revert_count} PRs as reverted")
    else:
        print(f"[OK] No new reverts to link")

if __name__ == '__main__':
    try:
        collect_prs()
        collect_all_review_data()
        link_reverts()

        print("\n✅ Data collection complete!")
        print(f"   prs.csv: {PRS_CSV}")
        print(f"   pr_reviews.csv: {REVIEWS_CSV}")
        print(f"   pr_review_comments.csv: {REVIEW_COMMENTS_CSV}")
        print(f"   pr_comments.csv: {COMMENTS_CSV}")

    except KeyboardInterrupt:
        print("\n⚠️  Interrupted. Checkpoint files were saved. Re-run to resume.")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)
