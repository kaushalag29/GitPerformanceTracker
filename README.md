# PostHog Engineering Impact Dashboard

> **Measure engineering impact beyond commits and lines of code.**
>
> A comprehensive analysis system that identifies the most impactful engineers at PostHog using a multi-dimensional scoring model that evaluates PR complexity, code quality, reviews, collaboration, and shipping velocity.

---

## Problem Statement

Traditional metrics like **lines of code, commit counts, and files changed** do not accurately reflect an engineer's true impact. This dashboard answers the question: **"Who are the most impactful engineers at PostHog?"** using thoughtful, multi-dimensional analysis that rewards:

- **Complex problem-solving** — shipping hard things, not just making small changes
- **Code quality** — getting it right the first time, not just shipping
- **Mentorship** — lifting the bar through code reviews
- **Collaboration** — knowledge transfer and teamwork
- **Shipping velocity** — consistent, reliable delivery

The dashboard is designed for busy engineering leaders who need a glanceable, credible answer backed by transparent methodology.

---

## Solution Overview

### Key Innovation: LLM-Powered Complexity & Quality Analysis

Instead of gaming metrics with LOC or commit counts, each PR is analyzed by an LLM to rate:
- **Complexity (1-5)**: How hard was the underlying problem? (scope, depth, multi-system coordination)
- **Quality (1-5)**: How well was it executed? (reviewer feedback, revision cycles, reverts, clarity)

The LLM considers:
- PR title, description, and code diff
- Inline review comments and code feedback
- Discussion comments from reviewers
- Whether the PR was later reverted (strong quality signal)

---

## Workflow & Architecture

### End-to-End Flow

```
┌─────────────────────────────────────────────────────────┐
│ STEP 1: Data Collection (Script 1)                      │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│ • Fetch last 90 days of merged PRs from GitHub API      │
│ • Collect PR metadata (title, body, author, stats)      │
│ • Collect reviews (reviewer, state, body)               │
│ • Collect inline review comments (code feedback)        │
│ • Collect discussion comments (team feedback)           │
│ • Detect reverts (PR title or body contains revert)     │
│ • Output: prs.csv, pr_reviews.csv,                      │
│           pr_review_comments.csv, pr_comments.csv       │
│ • Checkpoint: Re-run only fetches new PRs               │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ STEP 2: LLM Analysis (Script 2)                         │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│ • Load all PR data with review context                  │
│ • For each unanalyzed PR:                               │
│   - Build rich context (PR desc + reviews + diff)       │
│   - Call OpenRouter LLM (deepseek-v4-flash:free)        │
│   - Extract complexity_score (1-5)                      │
│   - Extract quality_score (1-5)                         │
│   - Store results with model version & timestamp        │
│ • Output: pr_analysis.csv                               │
│ • Checkpoint: Only analyzes unprocessed PRs             │
│ • Rate Limit: 20 req/min, 1000 req/day (w/ $10 credit) │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ STEP 3: Score Calculation (Script 3)                    │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│ • Load pr_analysis.csv (analyzed PRs only)              │
│ • For each author, aggregate:                           │
│   - avg_complexity_score × merged_pr_count              │
│   - avg_quality_score × merged_pr_count                 │
│   - reviews_given (count of reviews by author)          │
│   - comments_on_others_prs (collaboration signal)       │
│   - avg_turnaround_hours (velocity metric)              │
│ • Apply 5-dimension scoring formula (see below)         │
│ • Min-max normalize within cohort (0-1 scale)           │
│ • Apply revert penalty (-5 pts per revert, max -20)     │
│ • Output: contributors.csv with all breakdowns          │
│ • Scalable: Works with partial analysis data            │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ STEP 4: Dashboard (Streamlit)                           │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
│ • Load pre-computed contributors.csv (instant load)     │
│ • Hierarchical navigation using session state:          │
│   - Page 1: Interactive leaderboard (clickable rows)    │
│   - Page 2: Engineer detail page:                       │
│     * Score breakdown chart (complexity, quality, etc)  │
│     * 12 KPI metrics (impact, complexity, quality, etc) │
│     * Detailed PR list with parse error warnings        │
│     * Parse error explanation & solution               │
│     * Detailed calculation breakdown (collapsible)      │
│   - Methodology explainer (transparent scoring)         │
│ • Zero API calls at runtime (fast & reliable)           │
│ • Deployed on Streamlit Cloud                           │
└─────────────────────────────────────────────────────────┘
```

---

## Impact Score Formula

The final **Impact Score (0-100)** is calculated as:

```
Impact Score = (
    complexity_adjusted_output × 30% +
    quality_adjusted_output × 25% +
    reviews_given × 20% +
    comments_on_others_prs × 15% +
    merge_velocity × 10% -
    revert_penalty
)
```

### Score Dimensions Explained

| Dimension | Weight | Raw Signal | Why This Matters |
|---|---|---|---|
| **Complexity × Output** | 30% | `avg_complexity_score × merged_pr_count` | Rewards shipping impactful, hard problems. Prevents gaming via trivial changes. |
| **Quality × Output** | 25% | `avg_quality_score × merged_pr_count` | Rewards doing things right the first time. Penalizes sloppy merged code. |
| **Reviews Given** | 20% | Count of reviews given to other authors' PRs | Mentorship signal. Great engineers raise the bar for everyone. |
| **Collaboration Depth** | 15% | Comments on others' PRs (excluding self) | Knowledge transfer and unblocking teammates. |
| **Merge Velocity** | 10% | `merged_pr_count / avg_turnaround_hours_capped` (max 720h) | Reliable, consistent shipping cadence. |
| **Revert Penalty** | -5/PR | Count of reverted PRs (max -20 pts) | Strong negative signal. Reverts indicate broken code or fundamental issues. |

### Normalization

Each raw signal is **min-max normalized** within the cohort (0-1 scale), then scaled to its weight:

```
normalized_signal = (signal - min_signal) / (max_signal - min_signal)
dimension_score = normalized_signal × weight
```

This ensures fairness across contributors—top performer gets 100% of the weight, lowest gets 0%, others distributed proportionally.

### Revert Tracking

- A revert is detected by: `"Revert" in PR title` OR `"this reverts commit" in PR body`
- When a PR is reverted, its LLM quality score is also low (LLM sees `was_reverted=True`)
- Effect: Double penalty—lower quality score + explicit revert penalty

---

## Complete Setup Instructions (Tested on Mac M2 16GB)

### System Requirements

- **OS**: macOS (tested on M2 arm64, also compatible with Intel Macs)
- **Python**: 3.13+ (required for pandas 3.0.3+)
- **RAM**: 16GB minimum (8GB possible but tight)
- **Disk**: ~1 GB free (for data CSVs + venv)
- **API Credentials**:
  - GitHub personal access token (with repo read access)
  - OpenRouter API key with $10+ credit (1000 req/day quota)

### Step 1: Verify Python Installation

```bash
# Check Python version (must be 3.13+)
python3 --version
# Expected output: Python 3.13.x

# If you need Python 3.13, install via Homebrew (Mac)
brew install python@3.13
```

If you don't have Homebrew:
```bash
# Install Homebrew first
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# Then install Python
brew install python@3.13
```

**Verify after install:**
```bash
python3.13 --version
which python3.13
```

---

### Step 2: Clone/Setup Repository

```bash
# Navigate to where you want the project
cd ~/Downloads  # or your preferred directory

# If you haven't already:
git clone <your-repo-url>  # or create locally
cd GitPerformanceTracker
```

**Verify structure:**
```bash
ls -la
# Should see: README.md, Task.md, requirements.txt, scripts/, dashboard/, data/
```

---

### Step 3: Create Virtual Environment

```bash
# Navigate to project directory
cd /path/to/GitPerformanceTracker

# Create virtual environment with Python 3.13
python3.13 -m venv gitpt

# Activate virtual environment
source gitpt/bin/activate

# You should see (gitpt) at the start of your prompt
# Example: (gitpt) user@machine GitPerformanceTracker %
```

**To deactivate later:**
```bash
deactivate
```

---

### Step 4: Upgrade pip and Install Dependencies

```bash
# Ensure you're in the virtual environment (check for (gitpt) prefix)
# Upgrade pip first (important for newer packages)
pip install --upgrade pip setuptools wheel

# Install all dependencies
pip install -r requirements.txt
```

**Expected packages:**
```
PyGithub==2.9.1
pandas==3.0.3
openrouter==0.9.1
streamlit==1.57.0
plotly==6.7.0
python-dotenv==1.2.2
```

**Verify installation:**
```bash
pip list | grep -E "PyGithub|pandas|openrouter|streamlit|plotly"
```

---

### Step 5: Configure API Keys

#### 5a. Get GitHub Token

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens)
2. Click "Generate new token" (Classic)
3. Select scopes: `repo` (full control of private repositories)
4. Copy the token (you'll only see it once)

#### 5b. Get OpenRouter API Key

1. Go to [openrouter.co/keys](https://openrouter.co/keys)
2. Create an API key
3. Add $10+ credit to your account (needed for 1000 req/day quota)
4. Copy the API key

#### 5c. Create .env File

```bash
# In project root directory
cp .env.example .env
```

**Edit .env with your credentials:**
```bash
# Use nano, vim, or your preferred editor
nano .env
```

**Add these lines:**
```
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Save and exit** (nano: Ctrl+O, Enter, Ctrl+X)

**Verify .env is in .gitignore** (so credentials don't leak):
```bash
cat .gitignore | grep .env
# Should output: .env
```

---

### Step 6: Run the Pipeline (Step-by-Step)

**Important**: Keep the virtual environment activated throughout!

#### Step 6a: Collect PR Data from GitHub

```bash
# Make sure (gitpt) is in your prompt
python scripts/1_collect_data.py
```

**Runtime**: ~1-2 minutes (depends on network)

**Output files created:**
```bash
ls -lh data/
# prs.csv
# pr_reviews.csv
# pr_review_comments.csv
# pr_comments.csv
```

**If you see rate limit errors** (403):
- GitHub rate limit: 60 req/hour (unauthenticated) or 5000 req/hour (authenticated)
- Verify GITHUB_TOKEN is set: `echo $GITHUB_TOKEN`
- Wait 1 hour, then re-run (script checkpoints, so it resumes)

---

#### Step 6b: Analyze PRs with LLM

```bash
# Make sure (gitpt) is still active
python scripts/2_analyze_complexity.py
```

**Runtime**:
- First batch (10-20 PRs): ~1-2 minutes
- Full analysis (6900 PRs): ~7 days (at 1000 req/day quota)
- **Important**: You can interrupt (Ctrl+C) and resume later. Checkpoints ensure no re-analysis.

**Intermediate checkpoints** (re-run to see progress):
```bash
# After a few PRs analyzed:
python scripts/2_analyze_complexity.py
# Will skip already-analyzed PRs and continue

# Check how many are done:
wc -l data/pr_analysis.csv
# Example: 1801 = 1 header + 1800 analyzed
```

**Test mode** (faster, analyzes 2 PRs per author):
```bash
python scripts/2_analyze_complexity.py --test
# Takes ~2 minutes, good for validation
```

**Common errors:**
- `"Rate limited (429)"` → OpenRouter daily quota hit. Re-run tomorrow.
- `"OPENROUTER_API_KEY not set"` → Verify .env file and `echo $OPENROUTER_API_KEY`
- `"Parse errors on PR #58495"` → Model output was malformed (expected with free-tier). Retries automatically.

---

#### Step 6c: Calculate Impact Scores

```bash
# Make sure (gitpt) is active
python scripts/3_evaluate_scores.py
```

**Runtime**: ~1-2 minutes

**Output file created:**
```bash
ls -lh data/contributors.csv
# Contains final impact scores for all engineers
```

**Notes**:
- Only processes analyzed PRs (partial analysis is OK)
- Re-run as more PRs are analyzed to update scores
- Normalization recalculates based on current cohort

---

#### Step 6d: View Dashboard Locally

```bash
# Make sure (gitpt) is active
streamlit run dashboard/app.py
```

**Open in browser:**
```bash
open http://localhost:8501
```

Or manually go to: **http://localhost:8501**

**First load**: ~2-3 seconds (data cached after first load)

**Features to explore**:
1. **Leaderboard** — Click any row to navigate to engineer detail page
2. **Engineer Detail** — See comprehensive breakdown of their impact:
   - 12 KPI metrics (impact score, complexity, quality, reviews, collaboration, velocity, etc.)
   - Parse error warning (if any LLM failures occurred)
   - Score breakdown chart showing component contributions
   - Detailed calculation breakdown (collapsible, collapsed by default)
   - PR table with all analyzed PRs, sorted by complexity
3. **Methodology** — Expand bottom section on leaderboard to understand scoring

**To stop the dashboard:**
```bash
# In terminal: Ctrl+C
# Server shuts down gracefully
```

---

### Step 7: Full Pipeline Recap

For reference, here's the complete workflow:

```bash
# 1. Activate venv (if not already)
source gitpt/bin/activate

# 2. Collect data (1-2 min)
python scripts/1_collect_data.py

# 3. Analyze PRs with LLM (1-7 days, can resume)
python scripts/2_analyze_complexity.py

# 4. Calculate scores (1-2 min, run daily to update)
python scripts/3_evaluate_scores.py

# 5. View dashboard (Ctrl+C to stop)
streamlit run dashboard/app.py
```

---

### Advanced Options

#### Run with custom thread count (Script 1):
```bash
# Use 3 threads for data collection (default: 2)
python scripts/1_collect_data.py --threads 3
```

#### Run Script 2 in test mode:
```bash
# Analyze only 2 PRs per author (fast validation)
python scripts/2_analyze_complexity.py --test

# Or limit to 5 PRs per author
python scripts/2_analyze_complexity.py --limit 5
```

#### Check what's been analyzed:
```bash
# See how many PRs analyzed so far
wc -l data/pr_analysis.csv
# Output: 1801 = 1 header + 1800 data rows

# See contributors computed
wc -l data/contributors.csv
# Output: 281 = 1 header + 280 contributors
```

---

### Troubleshooting on Mac M2

| Issue | Cause | Solution |
|---|---|---|
| `python3.13: command not found` | Python 3.13 not installed | `brew install python@3.13` |
| `ModuleNotFoundError: No module named 'pandas'` | Dependencies not installed | `pip install -r requirements.txt` in venv |
| `GITHUB_TOKEN not set` | Missing .env or not loaded | Check `.env` exists, restart terminal after creating |
| `Rate limited (429)` | OpenRouter quota exhausted | Wait until next day, re-run |
| `venv not activated` | Using system Python instead of venv | Run `source gitpt/bin/activate` |
| `Permission denied` when running scripts | File not executable | `chmod +x scripts/*.py` (usually not needed) |
| Dashboard won't load (`Connection refused`) | Port 8501 in use | Kill process: `lsof -i :8501` then `kill -9 PID` |
| Memory issues (dataset too large) | 16GB RAM insufficient for all CSVs | Reduce --threads, or add RAM/swap |

---

### System Performance (Mac M2 16GB)

**Expected performance on Mac M2:**
- Script 1 (data collection): 1-2 min for 6900 PRs
- Script 2 (LLM analysis): 7 days for 6900 PRs @ 1000 req/day
  - Threading: 2 threads (safe for GitHub rate limits)
  - Memory usage: ~200MB per thread
  - CPU: Modest (waiting for API responses)
- Script 3 (scoring): <1 min for 280 contributors
- Dashboard: <3s first load, cached after

**RAM usage by task:**
- pandas operations: ~500MB-1GB (CSVs are large)
- Streamlit cache: ~200MB
- Total: ~2-3GB during peak (well under 16GB)

---

### What to Do Next

**Immediate** (first run):
1. Script 1: Collect data ✅
2. Script 2: Analyze (can take 7 days) ✅
3. Script 3: Calculate scores ✅
4. Dashboard: View and explore ✅

**While Script 2 is running** (over 7 days):
- Re-run Script 3 daily to see updated leaderboard
- Monitor PR analysis progress: `wc -l data/pr_analysis.csv`

**When complete**:
- Deploy dashboard to Streamlit Cloud (see below)
- Share public URL with team

---

### Deploy to Streamlit Cloud (5 minutes)

Once all data is collected:

```bash
# 1. Commit all data CSVs
git add data/*.csv
git commit -m "Add analyzed PR data"
git push

# 2. Go to share.streamlit.io
# 3. Click "New app"
# 4. Select repo, branch (main), file (dashboard/app.py)
# 5. Deploy
# 6. Share the public URL!
```

---

## API Limitations & Partial Data Note ⚠️

### OpenRouter Rate Limits

The system uses **OpenRouter free-tier models** with these constraints:

| Quota | Limit | Impact |
|---|---|---|
| **Per-model daily quota** | 200 req/day | Individual models hit limits quickly |
| **Account daily quota** | 1000 req/day (with $10 credit) | **Bottleneck for large analysis** |
| **Per-minute rate limit** | 20 req/min | Enforced in Script 2 |

### Current Data Status

- **Total PRs in PostHog/posthog (90 days)**: ~6,900
- **Currently analyzed**: 1,457 valid PRs (~21%) + 343 parse errors (19%)
- **Total analysis attempts**: 1,800 PRs
- **Remaining**: ~5,100 PRs

At 1000 req/day, full analysis will complete in **~7 days**.

**⚠️ Model Quality Issue**: 
- Current model (deepseek-v4-flash:free) produces inconsistent JSON output
- 343 PRs (19%) have `parse_error` in rationale fields
- These PRs default to complexity=3 and quality=3 (neutral scores), reducing accuracy
- Impact: Final impact scores may be skewed for engineers with analyzed PRs
- **Solution**: See Future Enhancement B (Upgrade to Claude Opus/Sonnet) to eliminate parse errors and improve accuracy

### How Dashboard Handles Partial Data

**Script 3 is designed to work with incomplete analysis:**

```python
# Only uses PRs that have been analyzed
if not analysis_df.empty:
    analyzed_pr_numbers = set(analysis_df['pr_number'].unique())
    prs_df = prs_df[prs_df['pr_number'].isin(analyzed_pr_numbers)].copy()
```

✅ **What you see on the dashboard**:
- Impact scores based only on analyzed PRs
- Leaderboard ranked by impact (normalized within analyzed cohort)
- Clickable rows for engineer deep-dive details

⚠️ **What's incomplete**:
- Final scores may shift when remaining 75% is analyzed
- High-velocity authors may rank differently when more PRs are analyzed
- Collaboration metrics only include analyzed PRs

**Recommendation**: Re-run Script 3 daily to update rankings as more PRs are analyzed.

---

## Technical Stack

| Component | Technology | Why |
|---|---|---|
| **Data Collection** | PyGithub 2.9.1 | Official GitHub API wrapper, handles pagination & rate limits |
| **LLM Analysis** | OpenRouter (deepseek-v4-flash:free) | Free-tier model, 1M token context, good code understanding |
| **Data Processing** | Pandas 3.0.3 | Efficient CSV operations, min-max normalization |
| **Dashboard** | Streamlit 1.57.0 | Fast iteration, live reloading, built-in caching |
| **Visualization** | Plotly 6.7.0 | Interactive charts (stacked bar breakdown) |
| **Environment** | Python 3.13, .env | Version pinning, secrets management |

---

## Data Files

| File | Rows | Description |
|---|---|---|
| `prs.csv` | ~6,900 | All merged PRs: number, title, body, author, additions, deletions, changed_files, commits, turnaround_hours, was_reverted |
| `pr_reviews.csv` | ~8,000 | Review activity: pr_number, reviewer, review_state (APPROVED/CHANGES_REQUESTED/COMMENTED), review_body |
| `pr_review_comments.csv` | ~20,000+ | Inline code comments: pr_number, reviewer, path, diff_hunk, comment_body |
| `pr_comments.csv` | ~50,000+ | Discussion comments: pr_number, commenter, pr_author, comment_body |
| `pr_analysis.csv` | ~1,800 (26%) | LLM output: pr_number, complexity_score (1-5), complexity_rationale, quality_score (1-5), quality_rationale, model_used, analyzed_at. Includes parse_error entries that are retried. |
| `contributors.csv` | ~280 authors | Final scores: author, merged_pr_count, reverted_pr_count, avg_complexity_score, avg_quality_score, reviews_given, comments_on_others_prs, score_complexity, score_quality, score_reviews, score_collaboration, score_velocity, revert_penalty, impact_score |

---

## Dashboard Features

### Page 1: Leaderboard
Interactive table with clickable rows and columns:
- **Rank**: Position in impact ranking
- **Engineer**: Author name (click row to navigate to detail page)
- **Impact Score**: 0-100 (normalized within cohort)
- **Analyzed PRs**: Count of merged PRs with LLM analysis
- **Avg Complexity**: 1-5 (LLM-rated average of successfully scored PRs)
- **Avg Quality**: 1-5 (LLM-rated average, excludes parse errors)
- **Reviews Given**: Count of code reviews completed for others
- **Comments on Others**: Collaboration metric (discussion participation)
- **Avg Turnaround (h)**: Hours from creation to merge
- **Reverts**: Count of reverted PRs

**Methodology Expander**: Explains the impact score formula, dimensions, normalization, complexity/quality definitions, data sources, and caveats.

### Page 2: Engineer Detail (Accessed by Clicking a Leaderboard Row)
Comprehensive engineer profile with:

#### Header & Navigation
- **Back button** to return to leaderboard
- **Engineer name and rank** (e.g., "#3 Alice")

#### Parse Error Warning (if applicable)
- Red error box if any analyzed PRs have LLM parse errors
- Explains cause: deepseek-v4-flash:free model output quality issues
- Recommends solution: Upgrade to Claude Opus/Sonnet

#### 12 KPI Metrics (across 3 rows)
- **Row 1**: Impact Score, Analyzed PRs (total), Successfully Scored, Avg Complexity
- **Row 2**: Avg Quality, Avg Turnaround, Reviews Given, Comments on Others' PRs
- **Row 3**: Avg Lines Added, Avg Lines Deleted, Avg Files Changed, Reverted PRs

**Important**: Complexity and quality averages on the engineer detail page are calculated from **successfully analyzed PRs only** (excluding parse error entries). This differs from the contributors.csv scores, which use all analyzed PRs. The engineer detail page shows the more accurate metrics by filtering out parse errors.

#### Score Breakdown Chart
Stacked bar chart showing contribution of each component:
- Complexity (0-30 max)
- Quality (0-25 max)
- Reviews (0-20 max)
- Collaboration (0-15 max)
- Velocity (0-10 max)

#### Detailed Calculation Breakdown (Collapsed by Default)
Shows:
- Raw signal definitions and calculations
- Min-max normalization explanation
- Actual numbers for this engineer
- Component scores and penalties
- Final impact score equation

#### PR Table
Markdown-rendered table of all **analyzed PRs only** (inner join with pr_analysis.csv):
- **PR #**: Clickable link to GitHub PR (displays as `[#56233](url)`)
- **Title**: PR name (truncated if >50 chars)
- **Complexity**: 1-5 score (or "⚠️ parse error")
- **Complexity Why**: LLM rationale (or parse error warning)
- **Quality**: 1-5 score (or "⚠️ parse error")
- **Quality Why**: LLM rationale (or parse error warning)
- **Hours to Merge**: Turnaround time
- **Reverted?**: Yes/No flag with emoji indicators:
  - 🟡 at end of row = PR has LLM parse error (score defaulted to 3/5)
  - 🔴 at end of row = PR was later reverted
- Sorted by complexity (descending)

### Methodology Section (on Leaderboard Page)
Expandable section explaining:
- Impact score formula (5 components + revert penalty)
- Why each dimension matters
- How LLM complexity/quality are rated (model, inputs, scale)
- Data sources (GitHub API, OpenRouter, pre-computed CSVs)
- Caveats (parse errors, LLM limitations, partial data)

---

## Deployment

### Local Testing
```bash
streamlit run dashboard/app.py
# Opens at http://localhost:8501
# <3s load time (all data pre-computed)
```

### Streamlit Cloud Deployment
1. Commit all data CSVs: `git add data/*.csv`
2. Push to GitHub
3. Go to [share.streamlit.io](https://share.streamlit.io)
4. "New app" → connect repo → select `main` branch → file `dashboard/app.py`
5. Set secrets (GITHUB_TOKEN, OPENROUTER_API_KEY) in Streamlit Cloud UI
6. Deploy → permanent public URL, always-on, <10s load

---

## Design Philosophy

- **Transparent methodology**: Every score component is visible and explained
- **No gaming metrics**: Complexity and quality are LLM-rated (can't inflate LOC). Reverts are real penalties.
- **Idempotent operations**: Scripts can be re-run safely. Checkpoints prevent duplicate fetching.
- **Pre-computation**: All analysis happens offline. Dashboard is pure display layer—fast and reliable.
- **Partial data handling**: Dashboard works with incomplete analysis and updates as more data is processed.
- **Scalable scoring**: Works with any number of analyzed PRs. Normalization adapts as cohort changes.

---

## Future Enhancements

The most impactful improvements involve upgrading the LLM model and improving error handling. Currently blocked by API costs and quota constraints.

### A. Upgrade to Powerful Reasoning Models (Best ROI)

**Problem**: Current model (deepseek-v4-flash:free) produces ~19% parse errors and inconsistent quality ratings.

**Solution**: Use Claude Opus or Claude Sonnet instead.
- **Claude Sonnet 4.6**: ~$35-70 for 6900 PRs (good ROI, ~1-day completion)
- **Claude Opus 4.7**: ~$100-140 for best quality
- **Benefits**: Eliminates parse errors, consistent JSON output, better accuracy on complex PRs

**Cost trade-off**: $35-140 upfront cost → eliminates 7-day quota bottleneck, improves score accuracy.

### B. Improved Parse Error Handling

**Current issues**: Script 2 retries parse errors and defaults to score=3. 19% of analyzed PRs affected.

**Solutions**:
1. **Stricter prompts** (1-2 hours, no cost) → reduce errors to 5-10%
2. **Better parsing logic** (2-3 hours, no cost) → reduce errors to 10-15%
3. **Switch to Claude** (same as A above) → reduce errors to <1%

**Recommendation**: Implement prompts improvement first (quick win), then upgrade model for long-term solution.

---

### C. Real-Time Data Updates (Currently Infeasible)

Use GitHub Actions to trigger Script 1 daily and commit new PR data, keeping the 90-day window fresh. Requires scheduling infrastructure and state tracking.

---

### D. Natural Language Query Interface

Add conversational interface to ask questions like "Who has the fastest merge times?" or "Compare Alice vs Bob on complexity". **Not critical** since the static leaderboard is intuitive and fast.

---

## Troubleshooting

### Script 1: Data Collection
- **Issue**: Rate limit hit (403 errors after many PRs)
  - Solution: Default 2 threads is safe; avoid `--threads 5+` for GitHub anti-abuse detection
- **Issue**: Missing review data
  - Solution: Ensure pr_reviews.csv, pr_review_comments.csv, pr_comments.csv all exist before Script 2

### Script 2: LLM Analysis
- **Issue**: "Rate limited (429)" message, no PRs analyzed
  - Solution: 1000 req/day quota exhausted. Wait until next day.
- **Issue**: Parse errors for some PRs
  - Solution: Script auto-retries with different API call attempts. Safe to re-run.
- **Issue**: Empty rationales in pr_analysis.csv
  - Solution: Verify OPENROUTER_API_KEY is set and has $10+ credit

### Script 3: Score Calculation
- **Issue**: "No analyzed PRs yet"
  - Solution: Run Script 2 to analyze at least 1 PR before running Script 3

### Dashboard
- **Issue**: Data not loading in Streamlit
  - Solution: Ensure all CSV files exist in `data/` directory
- **Issue**: Slow dashboard load
  - Solution: Clear Streamlit cache: `rm -rf ~/.streamlit/`

---

## Contributing

To extend this dashboard:

1. **Add new metric**: Modify Script 3's aggregation logic
2. **Add new visualization**: Add to `dashboard/app.py` (Plotly charts or Streamlit widgets)
3. **Improve LLM analysis**: Edit the system/user prompts in Script 2
4. **Optimize data collection**: Modify Script 1's fetch logic or rate limiting

---

## License & Attribution

Built for PostHog. Uses GitHub API, OpenRouter LLM, Streamlit, Pandas, Plotly.

---

## Quick Start for Production

1. **Collect data**: `python scripts/1_collect_data.py` (~1-2 min)
2. **Analyze PRs**: `python scripts/2_analyze_complexity.py` (7 days with quota, can pause/resume)
3. **Calculate scores**: `python scripts/3_evaluate_scores.py` (~1-2 min, re-run daily as analysis progresses)
4. **Deploy**: Push data CSVs and deploy `dashboard/app.py` to Streamlit Cloud
5. **Share**: Public URL available at `share.streamlit.io`

All data is pre-computed, dashboard loads in <3s with zero API calls at runtime.
