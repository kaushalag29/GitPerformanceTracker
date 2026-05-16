#!/usr/bin/env python3
"""
Script 3: Calculate final impact scores for each contributor
- Aggregates all dimensions (complexity, quality, reviews, collaboration, velocity)
- Applies min-max normalization within cohort
- Applies revert penalty
- Outputs contributors.csv with all component scores for dashboard display
"""

import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np

DATA_DIR = Path(__file__).parent.parent / "data"

PRS_CSV = DATA_DIR / "prs.csv"
ANALYSIS_CSV = DATA_DIR / "pr_analysis.csv"
REVIEWS_CSV = DATA_DIR / "pr_reviews.csv"
COMMENTS_CSV = DATA_DIR / "pr_comments.csv"
CONTRIBUTORS_CSV = DATA_DIR / "contributors.csv"

def minmax_normalize(series: pd.Series) -> pd.Series:
    """Min-max normalization: (x - min) / (max - min)."""
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(0.0, index=series.index)
    return (series - mn) / (mx - mn)

def calculate_scores():
    """Calculate impact scores for all contributors based on analyzed PRs."""
    print("[STEP 1] Loading data...")

    # Load all data files
    prs_df = pd.read_csv(PRS_CSV)
    analysis_df = pd.read_csv(ANALYSIS_CSV) if ANALYSIS_CSV.exists() else pd.DataFrame()
    reviews_df = pd.read_csv(REVIEWS_CSV) if REVIEWS_CSV.exists() else pd.DataFrame()
    comments_df = pd.read_csv(COMMENTS_CSV) if COMMENTS_CSV.exists() else pd.DataFrame()

    print(f"  → Loaded {len(prs_df)} total PRs")
    print(f"  → Loaded {len(analysis_df)} PR analyses")
    print(f"  → Loaded {len(reviews_df)} reviews")
    print(f"  → Loaded {len(comments_df)} comments")

    # **SCALABLE:** Only use analyzed PRs (filter prs_df to only include analyzed PRs)
    if not analysis_df.empty:
        analyzed_pr_numbers = set(analysis_df['pr_number'].unique())
        prs_df = prs_df[prs_df['pr_number'].isin(analyzed_pr_numbers)].copy()

        # Merge PR data with analysis scores
        prs_df = prs_df.merge(
            analysis_df[['pr_number', 'complexity_score', 'quality_score']],
            on='pr_number',
            how='inner'  # Inner join: only keep analyzed PRs
        )

        # CRITICAL: Deduplicate by PR number (checkpoint retries can create duplicates)
        prs_before_dedup = len(prs_df)
        prs_df = prs_df.drop_duplicates(subset=['pr_number'], keep='first')
        prs_after_dedup = len(prs_df)
        if prs_before_dedup != prs_after_dedup:
            print(f"  ⚠️  Removed {prs_before_dedup - prs_after_dedup} duplicate PR entries")

        print(f"  ✓ Using only {len(prs_df)} analyzed PRs (partial analysis mode)")
        print(f"  ℹ️  {len(pd.read_csv(PRS_CSV)) - len(prs_df)} PRs pending analysis")
    else:
        print("[WARN] No analyzed PRs yet. Run script 2 first.")
        print("[WARN] Cannot calculate scores without analysis data.")
        return pd.DataFrame()


    # Early exit if no analyzed data
    if prs_df.empty:
        return pd.DataFrame()

    # Convert was_reverted to boolean
    prs_df['was_reverted'] = prs_df['was_reverted'].astype(str).str.lower() == 'true'

    print("\n[STEP 2] Aggregating contributor stats...")

    # Get unique authors
    authors = prs_df['author'].unique()
    contributors = []

    for author in authors:
        author_prs = prs_df[prs_df['author'] == author]

        merged_pr_count = len(author_prs)
        reverted_pr_count = author_prs['was_reverted'].sum()
        avg_complexity = author_prs['complexity_score'].mean()
        avg_quality = author_prs['quality_score'].mean()
        avg_turnaround = author_prs['turnaround_hours'].mean()

        # Cap turnaround at 720 hours (30 days) for velocity calculation
        avg_turnaround_capped = min(avg_turnaround, 720)

        # Count reviews given
        author_reviews = reviews_df[reviews_df['reviewer'] == author]
        reviews_given = len(author_reviews)

        # Count comments on others' PRs
        author_comments = comments_df[
            (comments_df['commenter'] == author) &
            (comments_df['commenter'] != comments_df['pr_author'])
        ]
        comments_on_others = len(author_comments)

        contributors.append({
            'author': author,
            'merged_pr_count': merged_pr_count,
            'reverted_pr_count': int(reverted_pr_count),
            'avg_complexity_score': round(avg_complexity, 2),
            'avg_quality_score': round(avg_quality, 2),
            'reviews_given': reviews_given,
            'comments_on_others_prs': comments_on_others,
            'avg_turnaround_hours': round(avg_turnaround, 1),
            'avg_turnaround_hours_capped': avg_turnaround_capped,
        })

    contributors_df = pd.DataFrame(contributors)
    print(f"  → Computed stats for {len(contributors_df)} contributors")

    print("\n[STEP 3] Calculating impact scores...")

    # Calculate raw signals
    contributors_df['complexity_adjusted_output'] = (
        contributors_df['avg_complexity_score'] * contributors_df['merged_pr_count']
    )

    contributors_df['quality_adjusted_output'] = (
        contributors_df['avg_quality_score'] * contributors_df['merged_pr_count']
    )

    contributors_df['velocity_score'] = (
        contributors_df['merged_pr_count'] / contributors_df['avg_turnaround_hours_capped']
    )

    # Normalize each dimension to 0-1, then multiply by weight
    contributors_df['score_complexity'] = (
        minmax_normalize(contributors_df['complexity_adjusted_output']) * 30
    )

    contributors_df['score_quality'] = (
        minmax_normalize(contributors_df['quality_adjusted_output']) * 25
    )

    contributors_df['score_reviews'] = (
        minmax_normalize(contributors_df['reviews_given']) * 20
    )

    contributors_df['score_collaboration'] = (
        minmax_normalize(contributors_df['comments_on_others_prs']) * 15
    )

    contributors_df['score_velocity'] = (
        minmax_normalize(contributors_df['velocity_score']) * 10
    )

    # Revert penalty: -5 points per reverted PR, max -20 points
    contributors_df['revert_penalty'] = (
        contributors_df['reverted_pr_count'] * 5
    ).clip(upper=20)

    # Final impact score
    contributors_df['impact_score'] = (
        contributors_df['score_complexity'] +
        contributors_df['score_quality'] +
        contributors_df['score_reviews'] +
        contributors_df['score_collaboration'] +
        contributors_df['score_velocity'] -
        contributors_df['revert_penalty']
    ).clip(lower=0)

    # Round for display
    contributors_df['impact_score'] = contributors_df['impact_score'].round(1)
    contributors_df['score_complexity'] = contributors_df['score_complexity'].round(1)
    contributors_df['score_quality'] = contributors_df['score_quality'].round(1)
    contributors_df['score_reviews'] = contributors_df['score_reviews'].round(1)
    contributors_df['score_collaboration'] = contributors_df['score_collaboration'].round(1)
    contributors_df['score_velocity'] = contributors_df['score_velocity'].round(1)

    # Sort by impact score descending
    contributors_df = contributors_df.sort_values('impact_score', ascending=False)

    # Select columns for output CSV
    output_cols = [
        'author', 'merged_pr_count', 'reverted_pr_count', 'avg_complexity_score',
        'avg_quality_score', 'reviews_given', 'comments_on_others_prs',
        'avg_turnaround_hours', 'score_complexity', 'score_quality', 'score_reviews',
        'score_collaboration', 'score_velocity', 'revert_penalty', 'impact_score'
    ]

    contributors_df[output_cols].to_csv(CONTRIBUTORS_CSV, index=False)

    print(f"[OK] Scores calculated and saved to {CONTRIBUTORS_CSV}")

    # Print top 10 for validation
    print("\n" + "="*80)
    print("TOP 10 CONTRIBUTORS BY IMPACT SCORE")
    print("="*80)
    top10 = contributors_df.head(10)
    for idx, (_, row) in enumerate(top10.iterrows(), 1):
        print(f"\n#{idx} {row['author']}")
        print(f"  Impact Score: {row['impact_score']:.1f}/100")
        print(f"  Merged PRs: {row['merged_pr_count']} (avg complexity: {row['avg_complexity_score']:.1f}, avg quality: {row['avg_quality_score']:.1f})")
        print(f"  Reviews given: {row['reviews_given']}")
        print(f"  Comments on others' PRs: {row['comments_on_others_prs']}")
        if row['reverted_pr_count'] > 0:
            print(f"  ⚠️  Reverted PRs: {row['reverted_pr_count']} (penalty: {row['revert_penalty']:.0f} pts)")
        print(f"  Breakdown: complexity={row['score_complexity']:.1f} + quality={row['score_quality']:.1f} + reviews={row['score_reviews']:.1f} + collaboration={row['score_collaboration']:.1f} + velocity={row['score_velocity']:.1f}")
    print("\n" + "="*80)

    return contributors_df

if __name__ == '__main__':
    try:
        print("="*80)
        print("CALCULATE IMPACT SCORES (Scalable - uses analyzed PRs only)")
        print("="*80 + "\n")

        df = calculate_scores()

        if df.empty:
            print("\n⚠️  No analyzed PRs found. Run script 2 to analyze PRs first.")
            sys.exit(0)

        print("\n✅ Score calculation complete!")
        print(f"\nSummary (based on analyzed PRs):")
        print(f"  Total contributors: {len(df)}")
        print(f"  Top score: {df['impact_score'].max():.1f}")
        print(f"  Average score: {df['impact_score'].mean():.1f}")

    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
