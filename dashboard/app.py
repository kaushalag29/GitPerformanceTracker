#!/usr/bin/env python3
"""
Engineering Impact Dashboard for PostHog
Hierarchical dashboard: Leaderboard → Engineer Detail (clickable rows)
Metrics calculated from analyzed PRs only (Script 2 output as source of truth)
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
from datetime import datetime

# Page config
st.set_page_config(
    page_title="PostHog Engineering Impact Dashboard",
    page_icon="🦔",
    layout="wide",
    initial_sidebar_state="collapsed"
)

DATA_DIR = Path(__file__).parent.parent / "data"

@st.cache_data(ttl=3600)
def load_data():
    """Load all CSV data files."""
    try:
        contributors_df = pd.read_csv(DATA_DIR / "contributors.csv")
        prs_df = pd.read_csv(DATA_DIR / "prs.csv")
        analysis_df = pd.read_csv(DATA_DIR / "pr_analysis.csv")
        reviews_df = pd.read_csv(DATA_DIR / "pr_reviews.csv")
        comments_df = pd.read_csv(DATA_DIR / "pr_comments.csv")
        return contributors_df, prs_df, analysis_df, reviews_df, comments_df
    except FileNotFoundError as e:
        st.error(f"❌ Data file not found: {e}")
        st.stop()

# Load data
contributors_df, prs_df, analysis_df, reviews_df, comments_df = load_data()

# Initialize session state
if "selected_engineer" not in st.session_state:
    st.session_state.selected_engineer = None


# ===== PAGE 1: LEADERBOARD =====
def show_leaderboard():
    st.title("🦔 PostHog Engineering Impact Dashboard")
    st.caption("Last 90 days · PostHog/posthog · Analyze engineer contributions beyond commits")

    # Methodology
    st.info(
        "**Score = 30% complexity×output + 25% quality×output + 20% reviews given + "
        "15% collaborations + 10% velocity − revert penalty**\n\n"
        "All scores normalized within the cohort. Metrics shown below are based on analyzed PRs only "
        "(Script 2). Unanalyzed PRs are excluded to ensure proper attribution."
    )

    st.subheader("📊 Engineer Leaderboard")
    st.caption("👆 Click any row to explore an engineer's contributions in detail")

    # Prepare leaderboard display
    display_cols = [
        'author', 'impact_score', 'merged_pr_count', 'avg_complexity_score',
        'avg_quality_score', 'reviews_given', 'comments_on_others_prs',
        'avg_turnaround_hours', 'reverted_pr_count'
    ]

    leaderboard_display = contributors_df[display_cols].copy()
    leaderboard_display.insert(0, 'Rank', range(1, len(leaderboard_display) + 1))
    leaderboard_display.columns = [
        'Rank', 'Engineer', 'Impact Score', 'Analyzed PRs', 'Avg Complexity',
        'Avg Quality', 'Reviews Given', 'Comments on Others', 'Avg Turnaround (h)', 'Reverts'
    ]

    # Clickable leaderboard using session state
    event = st.dataframe(
        leaderboard_display,
        column_config={
            "Impact Score": st.column_config.NumberColumn(format="%.1f"),
            "Avg Complexity": st.column_config.NumberColumn(format="%.1f"),
            "Avg Quality": st.column_config.NumberColumn(format="%.1f"),
            "Avg Turnaround (h)": st.column_config.NumberColumn(format="%.1f"),
        },
        on_select="rerun",
        selection_mode="single-row",
        width='stretch',
        hide_index=True,
    )

    if event.selection.rows:
        row_idx = event.selection.rows[0]
        st.session_state.selected_engineer = leaderboard_display.iloc[row_idx]['Engineer']
        st.rerun()

    # Methodology expander
    with st.expander("📖 Methodology & Score Explanation"):
        st.markdown("""
        ### Impact Score Formula

        The impact score is designed to reward multi-dimensional engineering excellence:

        | Dimension | Weight | What It Measures |
        |---|---|---|
        | **Complexity × Output** | 30% | Average complexity (LLM-rated 1-5) × number of PRs merged. Rewards shipping hard things. |
        | **Quality × Output** | 25% | Average quality (LLM-rated 1-5) × number of PRs merged. Rewards doing things right first time. |
        | **Reviews Given** | 20% | Number of code reviews completed for others. Signals mentorship and team impact. |
        | **Collaboration Depth** | 15% | Comments left on others' PRs (excluding self). Shows knowledge transfer and teamwork. |
        | **Merge Velocity** | 10% | PR count ÷ average hours to merge. Rewards consistent, quick shipping. |
        | **Revert Penalty** | -5 per PR | Each reverted PR subtracts 5 points (max -20). Production rollbacks are negative signals. |

        ### Scoring Details

        - **Normalization**: Each raw signal is min-max normalized within the cohort (0-1), then scaled to its weight.
        - **No minimum threshold**: All contributors with ≥1 merged PR are included. Low-contribution authors naturally score lower.
        - **Complexity & Quality**: Rated by LLM (`deepseek-v4-flash:free` via OpenRouter) using:
          - PR title and full description (no truncation)
          - Code diff (full diff, no truncation)
          - Review feedback (inline comments + discussion)
          - Whether the PR was later reverted
        - **Revert signal**: If a PR is later reverted, both the revert flag AND the quality score reflect the rollback.

        ### Data Sources

        - **GitHub API**: PyGithub library, fetching last 90 days of merged PRs from PostHog/posthog
        - **LLM Analysis**: OpenRouter free-tier deepseek-v4-flash:free, JSON-structured prompts
        - **All data**: Pre-computed, committed to repo. Dashboard loads from CSV (zero API calls at runtime).

        ### Caveats

        - **Complexity & Quality** are heuristics based on LLM analysis of PR metadata and code diff.
        - **Parse errors** (19% of analyzed PRs): Free-tier deepseek model sometimes fails to return structured JSON. Scores default to 3/5. See engineer detail page for parse error details.
        - **Reverts** tracked by "Revert" in PR title or "this reverts commit" in body.
        - **Bot PRs** (renovate, dependabot, etc.) are excluded to avoid inflating counts with dependency updates.
        """)

    st.markdown("---")
    st.caption(
        f"Dashboard updated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"Data window: last 90 days · "
        f"Built with Streamlit + PostHog GitHub data"
    )


# ===== PAGE 2: ENGINEER DETAIL =====
def show_engineer_detail():
    selected_author = st.session_state.selected_engineer

    # Back button
    col1, col2 = st.columns([2, 8])
    with col1:
        if st.button("← Back to Leaderboard", width='stretch'):
            st.session_state.selected_engineer = None
            st.rerun()

    # Get contributor row for context
    selected_row = contributors_df[contributors_df['author'] == selected_author].iloc[0]
    rank = contributors_df.index.get_loc(
        contributors_df[contributors_df['author'] == selected_author].index[0]
    ) + 1

    st.title(f"#{rank} {selected_author}")


    # ===== DATA PREPARATION =====
    # Only analyzed PRs for this engineer (inner join) — excludes script 1 unanalyzed PRs
    author_prs = prs_df[prs_df['author'] == selected_author].copy()
    author_analyzed = author_prs.merge(
        analysis_df[['pr_number', 'complexity_score', 'complexity_rationale',
                     'quality_score', 'quality_rationale']],
        on='pr_number',
        how='inner'  # CRITICAL: only analyzed PRs
    )

    # Split: successful vs parse errors
    parse_error_mask = (
        (author_analyzed['complexity_rationale'] == 'parse_error') |
        (author_analyzed['quality_rationale'] == 'parse_error')
    )
    successful_prs = author_analyzed[~parse_error_mask]
    parse_error_prs = author_analyzed[parse_error_mask]

    # ===== PARSE ERROR WARNING =====
    if len(parse_error_prs) > 0:
        pct = len(parse_error_prs) / len(author_analyzed) * 100
        st.error(
            f"⚠️ **{len(parse_error_prs)} of {len(author_analyzed)} PRs ({pct:.0f}%) have LLM parse errors** — "
            f"scores defaulted to 3/5. Complexity and quality averages below exclude these PRs. "
            f"\n\n**Cause**: `deepseek-v4-flash:free` (free-tier model) has limited reasoning capability and often fails "
            f"to return properly structured JSON output, resulting in fallback scores. "
            f"\n\n**Solution**: Upgrading to Claude Opus/Sonnet would eliminate this issue and improve accuracy."
        )

    # ===== METRICS ROWS =====
    # Calculate metrics from successfully analyzed PRs
    avg_complexity_success = successful_prs['complexity_score'].mean() if len(successful_prs) > 0 else 0
    avg_quality_success = successful_prs['quality_score'].mean() if len(successful_prs) > 0 else 0
    avg_turnaround = author_analyzed['turnaround_hours'].mean()
    avg_additions = author_analyzed['additions'].mean()
    avg_deletions = author_analyzed['deletions'].mean()
    avg_files_changed = author_analyzed['changed_files'].mean()

    # Reviews and comments from data
    author_reviews = reviews_df[reviews_df['reviewer'] == selected_author]
    reviews_given = len(author_reviews)
    author_comments = comments_df[
        (comments_df['commenter'] == selected_author) &
        (comments_df['commenter'] != comments_df['pr_author'])
    ]
    comments_on_others = len(author_comments)

    # Row 1: Impact metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Impact Score", f"{selected_row['impact_score']:.1f}", "/100")
    with col2:
        st.metric("Analyzed PRs (Output)", int(len(author_analyzed)), "PRs merged")
    with col3:
        st.metric("Successfully Scored", int(len(successful_prs)), f"exclude {len(parse_error_prs)} errors")
    with col4:
        st.metric("Avg Complexity", f"{avg_complexity_success:.1f}", "/5")

    # Row 2: Quality and collaboration
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Avg Quality", f"{avg_quality_success:.1f}", "/5")
    with col2:
        st.metric("Avg Turnaround", f"{avg_turnaround:.1f}h")
    with col3:
        st.metric("Reviews Given", int(reviews_given))
    with col4:
        st.metric("Comments on Others' PRs", int(comments_on_others))

    # Row 3: Code impact
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Avg Lines Added", f"{avg_additions:.0f}")
    with col2:
        st.metric("Avg Lines Deleted", f"{avg_deletions:.0f}")
    with col3:
        st.metric("Avg Files Changed", f"{avg_files_changed:.1f}")
    with col4:
        st.metric("Reverted PRs", int(selected_row['reverted_pr_count']))

    # ===== SCORE BREAKDOWN CHART =====
    st.subheader("Score Breakdown")

    components = [
        ('Complexity', selected_row['score_complexity']),
        ('Quality', selected_row['score_quality']),
        ('Reviews', selected_row['score_reviews']),
        ('Collaboration', selected_row['score_collaboration']),
        ('Velocity', selected_row['score_velocity']),
    ]

    labels, values = zip(*components)
    remaining = [max(0, 30 - values[0]), max(0, 25 - values[1]), max(0, 20 - values[2]),
                 max(0, 15 - values[3]), max(0, 10 - values[4])]

    fig = go.Figure(data=[
        go.Bar(name='Score', x=labels, y=values, marker_color='#1f77b4'),
        go.Bar(name='Remaining', x=labels, y=remaining, marker_color='#d3d3d3'),
    ])

    fig.update_layout(
        barmode='stack',
        title=f"Score Breakdown (Total: {selected_row['impact_score']:.1f}/100)",
        yaxis_title="Points",
        height=300,
        margin=dict(l=0, r=0, t=40, b=0),
        showlegend=False,
    )
    st.plotly_chart(fig, width='stretch')

    # ===== DETAILED CALCULATION BREAKDOWN =====
    with st.expander("🔍 Detailed Calculation Breakdown", expanded=False):
        # Show formula explanation
        st.markdown("""
        ### Impact Score Calculation (min-max normalized within cohort, max 100)

        **Raw Signals:**
        - Complexity Signal = avg_complexity_score × merged_pr_count
        - Quality Signal = avg_quality_score × merged_pr_count
        - Reviews Signal = count of code reviews given
        - Collaboration Signal = comments on others' PRs
        - Velocity Signal = merged_pr_count ÷ avg_turnaround_hours

        **Weighted Components (normalized 0-1, then scaled):**
        - Score Complexity = minmax(complexity_signal) × **30**
        - Score Quality = minmax(quality_signal) × **25**
        - Score Reviews = minmax(reviews_signal) × **20**
        - Score Collaboration = minmax(collab_signal) × **15**
        - Score Velocity = minmax(velocity_signal) × **10**

        **Final Impact Score:**
        ```
        impact_score = (complexity + quality + reviews + collaboration + velocity) − revert_penalty
        revert_penalty = min(reverted_pr_count × 5, 20)
        impact_score = max(0, impact_score)
        ```
        """)

        st.markdown("---")
        st.markdown(f"### Calculation for {selected_author}")

        # Calculate raw signals
        complexity_signal = avg_complexity_success * len(author_analyzed)
        quality_signal = avg_quality_success * len(author_analyzed)
        velocity_signal = len(author_analyzed) / max(avg_turnaround, 1)

        st.markdown(f"""
        **Raw Signals for {selected_author}:**
        - Complexity Signal = {avg_complexity_success:.2f} (avg) × {len(author_analyzed)} (PRs) = **{complexity_signal:.2f}**
        - Quality Signal = {avg_quality_success:.2f} (avg) × {len(author_analyzed)} (PRs) = **{quality_signal:.2f}**
        - Reviews Signal = **{int(reviews_given)}** reviews given
        - Collaboration Signal = **{int(comments_on_others)}** comments on others' PRs
        - Velocity Signal = {len(author_analyzed)} (PRs) ÷ {avg_turnaround:.1f}h = **{velocity_signal:.2f}** PRs/hour

        **Component Scores (after min-max normalization & weighting):**
        - Score Complexity = {selected_row['score_complexity']:.1f}/30
        - Score Quality = {selected_row['score_quality']:.1f}/25
        - Score Reviews = {selected_row['score_reviews']:.1f}/20
        - Score Collaboration = {selected_row['score_collaboration']:.1f}/15
        - Score Velocity = {selected_row['score_velocity']:.1f}/10
        - **Subtotal** = {selected_row['score_complexity'] + selected_row['score_quality'] + selected_row['score_reviews'] + selected_row['score_collaboration'] + selected_row['score_velocity']:.1f}

        **Revert Penalty:**
        - Reverted PRs: {int(selected_row['reverted_pr_count'])}
        - Penalty: {int(selected_row['reverted_pr_count'])} × 5 = **{selected_row['revert_penalty']:.0f}** pts (capped at 20)

        **Final Impact Score:**
        - {selected_row['score_complexity'] + selected_row['score_quality'] + selected_row['score_reviews'] + selected_row['score_collaboration'] + selected_row['score_velocity']:.1f} − {selected_row['revert_penalty']:.0f} = **{selected_row['impact_score']:.1f}**/100
        """)

    # ===== PR TABLE =====
    st.subheader(f"📝 {selected_author}'s Analyzed PRs (sorted by complexity)")

    # Prepare PR data for display
    display_pr_cols = [
        'pr_number', 'title', 'complexity_score', 'complexity_rationale',
        'quality_score', 'quality_rationale', 'turnaround_hours', 'was_reverted'
    ]

    display_pr_data = author_analyzed[display_pr_cols].copy()

    # Store URLs separately and keep PR # as clean display
    pr_urls = display_pr_data['pr_number'].apply(
        lambda x: f"https://github.com/PostHog/posthog/pull/{int(x)}"
    )
    display_pr_data['_url'] = pr_urls
    display_pr_data['PR #'] = display_pr_data['pr_number'].apply(lambda x: f"#{int(x)}")

    display_pr_data = display_pr_data.drop('pr_number', axis=1)
    display_pr_data.columns = [
        'Title', 'Complexity', 'Complexity Why', 'Quality', 'Quality Why', 'Hours to Merge', 'Reverted?', '_url', 'PR #'
    ]

    # Reorder columns to put PR # first
    display_pr_data = display_pr_data[['PR #', 'Title', 'Complexity', 'Complexity Why', 'Quality', 'Quality Why', 'Hours to Merge', 'Reverted?', '_url']]

    # Replace parse_error with warning icon
    display_pr_data['Complexity Why'] = display_pr_data['Complexity Why'].apply(
        lambda x: '⚠️ parse error' if x == 'parse_error' else x
    )
    display_pr_data['Quality Why'] = display_pr_data['Quality Why'].apply(
        lambda x: '⚠️ parse error' if x == 'parse_error' else x
    )

    # Sort by complexity descending
    display_pr_data = display_pr_data.sort_values('Complexity', ascending=False, na_position='last')

    # Style for parse errors and reverted PRs
    def highlight_rows(row):
        is_parse_error = ('⚠️ parse error' in str(row['Complexity Why']) or
                         '⚠️ parse error' in str(row['Quality Why']))
        is_reverted = row['Reverted?'] == True or row['Reverted?'] == 'True'

        if is_parse_error:
            return ['background-color: #fff3cd'] * len(row)
        elif is_reverted:
            return ['background-color: #ffe6e6'] * len(row)
        else:
            return [''] * len(row)

    # Create markdown table with clickable PR # links
    markdown_table = "| PR # | Title | Complexity | Complexity Why | Quality | Quality Why | Hours to Merge | Reverted? |\n"
    markdown_table += "|------|-------|-----------|---|---------|---|---|---|\n"

    for _, row in display_pr_data.iterrows():
        pr_link = f"[{row['PR #']}]({row['_url']})"
        title = row['Title'][:50] + "..." if len(str(row['Title'])) > 50 else row['Title']
        complexity = f"{row['Complexity']:.1f}"
        complexity_why = row['Complexity Why'][:20] + "..." if len(str(row['Complexity Why'])) > 20 else row['Complexity Why']
        quality = f"{row['Quality']:.1f}"
        quality_why = row['Quality Why'][:20] + "..." if len(str(row['Quality Why'])) > 20 else row['Quality Why']
        turnaround = f"{row['Hours to Merge']:.1f}"
        reverted = "Yes" if row['Reverted?'] in [True, 'True'] else "No"

        # Apply parse error highlighting
        is_parse_error = ('⚠️ parse error' in str(row['Complexity Why']) or
                         '⚠️ parse error' in str(row['Quality Why']))
        is_reverted = row['Reverted?'] in [True, 'True']

        row_bg = " 🟡" if is_parse_error else (" 🔴" if is_reverted else "")

        markdown_table += f"| {pr_link} | {title} | {complexity} | {complexity_why} | {quality} | {quality_why} | {turnaround} | {reverted}{row_bg} |\n"

    st.markdown(markdown_table)

    st.caption(
        f"🟡 Yellow rows: PR has LLM parse error (score = 3/5 default, not analyzed). "
        f"🔴 Red rows: PR was later reverted. "
        f"Metrics above exclude {len(parse_error_prs)} parse error PR(s)."
    )


# ===== ROUTING =====
if st.session_state.selected_engineer:
    show_engineer_detail()
else:
    show_leaderboard()
