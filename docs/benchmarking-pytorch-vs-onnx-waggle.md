# Benchmarking PyTorch vs ONNX Runtime — Waggle-mcp Issue #123

> **GSSoC 2026 Contribution** | Waggle-mcp Repository | PR #276 | Status: ✅ Approved

A complete walkthrough of how I benchmarked embedding latency between SentenceTransformer's PyTorch backend and ONNX Runtime for the [Waggle-mcp](https://github.com/Abhigyan-Shekhar/Waggle-mcp) open-source project as part of GirlScript Summer of Code 2026.

---

## Table of Contents

- [Contribution Overview](#contribution-overview)
- [What is Waggle-mcp?](#what-is-waggle-mcp)
- [The Problem — What Was Issue #123 Asking For?](#the-problem--what-was-issue-123-asking-for)
- [Key Concepts](#key-concepts)
- [Repository Structure](#repository-structure)
- [Environment Setup](#environment-setup)
- [The Solution — Script Walkthrough](#the-solution--script-walkthrough)
  - [Constants](#61-constants)
  - [Generating Test Sentences](#62-generating-test-sentences)
  - [Benchmarking Single Text](#63-benchmarking-single-text)
  - [Benchmarking Batches](#64-benchmarking-batches)
  - [Putting It All Together](#65-main-function)
- [Errors I Hit and How I Fixed Them](#errors-i-hit-and-how-i-fixed-them)
- [The Bug CodeRabbit Found — P95 Off-by-One](#the-bug-coderabbit-found--p95-off-by-one)
- [Git and GitHub Workflow](#git-and-github-workflow)
- [PR Lifecycle](#pr-lifecycle)
- [Benchmark Results](#benchmark-results)
- [Linux Commands Reference](#linux-commands-reference)
- [Lessons Learned](#lessons-learned)
- [Final Checklist](#final-checklist)

---

## Contribution Overview

| Field | Details |
|-------|---------|
| **Contributor** | Aman Kumar Happy ([@amankumarhappy](https://github.com/amankumarhappy)) |
| **Program** | GSSoC 2026 (GirlScript Summer of Code) |
| **Repository** | [Abhigyan-Shekhar/Waggle-mcp](https://github.com/Abhigyan-Shekhar/Waggle-mcp) |
| **Issue** | [#123 — Benchmark embed latency: PyTorch vs ONNX Runtime](https://github.com/Abhigyan-Shekhar/Waggle-mcp/issues/123) |
| **Pull Request** | [#276 — perf(embeddings): benchmark embed latency...](https://github.com/Abhigyan-Shekhar/Waggle-mcp/pull/276) |
| **Branch** | `perf/benchmark-embed-latency-pytorch-vs-onnx` |
| **File Created** | `scripts/benchmark_embed.py` (381 lines) |
| **Commits** | 2 — initial script + P95 bug fix |
| **Final Status** | ✅ PR Open, Approved by maintainer, All CI Checks Passing |
| **Labels** | `gssoc:approved`, `SSoC26`, `good-first-issue`, `level:beginner` |
| **Reviewers** | @Abhigyan-Shekhar (Owner), @ard12 (Collaborator) |

---

## What is Waggle-mcp?

Waggle-mcp is an open-source AI-powered knowledge graph tool. It's an MCP (Model Context Protocol) server that helps AI assistants store, retrieve, and reason over structured knowledge — essentially a smart memory system for AI that lets you add information, search semantically, and track relationships between concepts.

Internally, Waggle-mcp uses an embedding model called `all-MiniLM-L6-v2` to convert text into numbers so that semantic similarity can be computed. This embedding engine is what makes search actually work.

**GSSoC** (GirlScript Summer of Code) is an open-source contribution program where students from across India contribute to real-world projects and earn points on a leaderboard. Issue #123 was labeled `level:beginner` and `good-first-issue` — a great entry point.

---

## The Problem — What Was Issue #123 Asking For?

The Waggle team was running their embedding model on the default PyTorch backend. There was a claim floating around that ONNX Runtime might be 2–5x faster. But there was **no actual data** — just an anecdotal claim.

Maintainer @ard12 wanted hard numbers before deciding anything about switching backends.

> **Simple analogy:** Imagine two cooks in a kitchen. Cook A (PyTorch) is the current default — reliable, well-known. Cook B (ONNX) claims to be 2–5x faster. But nobody has actually timed them making the same dish. Your job: be the person with the stopwatch. Time both, write down the numbers, present a clean table.

### Exact Acceptance Criteria from the Issue

| Requirement | What It Means |
|-------------|---------------|
| Write `scripts/benchmark_embed.py` | Create a new standalone Python script |
| Load `all-MiniLM-L6-v2` with both backends | PyTorch AND ONNX versions of the same model |
| Test batch sizes 1, 8, 32, 64 | Different amounts of text at once |
| 100 single iterations, 20 batch iterations | Enough runs for stable statistics |
| Report median, P95, throughput, memory | 4 specific metrics required |
| Print markdown table | Output must be pasteable into GitHub PR |
| Seed randomness | Same sentences every run = reproducible |
| Use warmup runs | Discard first N results — fair measurement |
| CPU only — no CUDA | Must run on a normal laptop without GPU |

---

## Key Concepts

Before diving into the code, here's what you need to understand:

**Embedding** — Converting text into a list of numbers (a vector). Example: `'fever medicine'` becomes `[0.23, 0.87, 0.12, ...]` — 384 numbers. Similar words get similar numbers. This lets computers compare *meaning*, not just spelling.

**all-MiniLM-L6-v2** — The specific embedding model used by Waggle-mcp. MiniLM = Mini Language Model (small but powerful). L6 = 6 transformer layers. v2 = version 2. It converts text into 384-dimensional vectors.

**PyTorch Backend** — The standard way to run the model. Well-known, flexible, but heavier. This was Waggle's default.

**ONNX Runtime** — ONNX = Open Neural Network Exchange. A universal optimized format. Like compiling Python to C++ — same output, but faster execution. SentenceTransformer supports it with `backend='onnx'`.

**Warmup Runs** — The first few runs of an ML model are slow because CPU cache is cold (like a bike engine on a cold morning). We run 5 iterations and *discard* results before measuring. Not cheating — this is standard scientific practice.

**Median Latency** — The middle value when all timings are sorted. If 100 runs: median is the 50th value. Not affected by extreme outliers. Much better than average for performance metrics.

**P95 Latency** — 95th percentile. 95% of requests were faster than this value. Represents the worst realistic case (not absolute worst). Production SLAs are typically defined using P95 or P99.

**Throughput** — How many texts can be processed per second. Formula: `texts/sec = batch_size * 1000 / median_ms`. Higher is better.

**tracemalloc** — Python's built-in memory tracker. More precise than `psutil` because it only tracks Python-level allocations, not OS-level overhead.

**gc.collect()** — Python's garbage collector, called manually before each test to establish a clean baseline. Ensures neither backend has leftover memory from a previous run.

---

## Repository Structure

```
Waggle-mcp/
├── src/waggle/
│   ├── embeddings.py        ← THE most important file — EmbeddingModel class lives here
│   └── server.py            ← MCP server (Issue #129 — future work)
├── scripts/
│   ├── benchmark_embed.py   ← THE FILE I CREATED (didn't exist before)
│   └── benchmark_token_efficiency.py  ← Existing example, studied for style reference
├── tests/
├── pyproject.toml           ← Project dependencies and build config
├── CONTRIBUTING.md          ← Rules for contributors — read this first!
└── .venv/                   ← Virtual environment — activate before any work
```

### Key File: `src/waggle/embeddings.py`

This file contains the `EmbeddingModel` class — the heart of Waggle's AI. Before writing any benchmark code, I read through this file carefully:

```bash
sed -n '1,220p' src/waggle/embeddings.py
```

The most important discovery was `batch_size = min(64, max(1, len(normalized)))` inside `embed_batch()`. This is exactly why the issue requested batch sizes `[1, 8, 32, 64]` — they mirror Waggle's real internal behavior.

---

## Environment Setup

All commands run inside WSL Ubuntu on Windows.

| Step | Command | What to Expect |
|------|---------|----------------|
| Navigate to repo | `cd ~/Open\ Source/Waggle-mcp` | Always start here — wrong directory = errors |
| Confirm location | `pwd` | Should show: `/home/aman_kumar_happy/Open Source/Waggle-mcp` |
| Activate venv | `source .venv/bin/activate` | `(venv)` appears at start of terminal line |
| Confirm Python | `which python` | Must show `.venv/bin/python` — not system Python |
| Install project | `pip install -e '.[dev]'` | Editable mode — imports work correctly |
| Install benchmark deps | `pip install sentence-transformers onnxruntime optimum psutil` | All needed for the script |
| Verify install | `python -c "import sentence_transformers; print(sentence_transformers.__version__)"` | Should print version number |
| Run benchmark | `python scripts/benchmark_embed.py` | Main command to execute |

> ⚠️ **Important:** The `.venv` folder already existed in the repo. Always activate it with `source .venv/bin/activate` before running anything. If you forget, you'll either install packages to the wrong Python or get import errors.

---

## The Solution — Script Walkthrough

The complete file is `scripts/benchmark_embed.py` — 381 lines. Here's what every section does and *why*.

### 6.1 Constants

All configuration lives at the top of the file as named constants. This makes the script easy to tweak without touching the logic.

```python
MODEL_NAME = 'all-MiniLM-L6-v2'  # The model being tested
BATCH_SIZES = [1, 8, 32, 64]      # Mirrors Waggle's real max batch size of 64
SINGLE_ITERATIONS = 100            # 100 runs gives stable median
BATCH_ITERATIONS = 20              # Batch is slower, 20 is enough
WARMUP_RUNS = 5                    # First 5 runs discarded (cold cache)
RANDOM_SEED = 42                   # Same seed = same sentences every run
```

### 6.2 Generating Test Sentences

Generates realistic health-related sentences using seeded randomness. Called **once** — same sentences reused across all tests for a fair comparison.

```python
def generate_test_sentences(num_sentences, seed=RANDOM_SEED):
    random.seed(seed)  # Fix randomness — same output every time
    health_topics = ['patient', 'doctor', 'hospital', ...]  # Domain vocabulary

    for _ in range(num_sentences):  # _ means "don't need the loop variable"
        sentence = f'The {adj} {topic} {verb} outcomes...'
        sentences.append(sentence)

    return sentences  # List of strings ready for embedding
```

Why health topics? Because Waggle-mcp is used in health-adjacent contexts. Using realistic domain vocabulary makes the benchmark more meaningful.

### 6.3 Benchmarking Single Text

This function times how long it takes to embed one sentence at a time, across 100 runs.

```python
def benchmark_single(model, sentence, backend_name):
    # PHASE 1: WARMUP (results discarded)
    for _ in range(WARMUP_RUNS):   # 5 runs
        model.encode(sentence)      # Just to warm up the CPU cache

    # PHASE 2: CLEAN SLATE
    gc.collect()          # Clear Python garbage from memory
    tracemalloc.start()   # Begin watching memory

    # PHASE 3: ACTUAL MEASUREMENT
    latencies = []
    for _ in range(SINGLE_ITERATIONS):   # 100 runs
        start = time.perf_counter()       # Start stopwatch (nanosecond precision)
        model.encode(sentence, normalize_embeddings=True, convert_to_numpy=True)
        end = time.perf_counter()         # Stop stopwatch
        latencies.append((end - start) * 1000)  # Store in milliseconds

    # PHASE 4: MEMORY READING
    _, peak = tracemalloc.get_traced_memory()  # _ = current (unused), peak = max
    tracemalloc.stop()

    # PHASE 5: STATISTICS
    median_latency = statistics.median(latencies)
    p95_latency = sorted(latencies)[int(0.95 * (len(latencies) - 1))]  # TRUE P95
    throughput = 1000.0 / median_latency  # texts per second
    memory_mb = peak / (1024 * 1024)      # bytes to megabytes
```

### 6.4 Benchmarking Batches

Same structure as `benchmark_single()`, but passes a **list** of sentences and calculates throughput differently.

```python
def benchmark_batch(model, sentences, batch_size, backend_name):
    batch = sentences[:batch_size]  # Take first N sentences

    # ... warmup, gc.collect(), tracemalloc — same as single ...

    model.encode(batch, ...)  # List input — processes all at once

    # Throughput formula changes for batch:
    throughput = (batch_size * 1000.0) / median_latency
    # Example: batch=32, median=85ms → 32*1000/85 = 376 texts/sec
```

The key insight here: throughput scales with batch size but doesn't scale linearly because of internal model overhead. This is exactly what the benchmark reveals.

### 6.5 Main Function

Loads both models, generates test data once, runs all combinations, and prints the results.

```python
def main():
    # Load BOTH models
    model_pytorch = SentenceTransformer(MODEL_NAME, device='cpu')
    model_onnx = SentenceTransformer(MODEL_NAME, backend='onnx', device='cpu')

    # Generate test data ONCE — reused across all tests (fair comparison)
    test_sentences = generate_test_sentences(max(BATCH_SIZES))  # 64 sentences

    # Loop over both backends
    for backend_name, model in [('PyTorch', model_pytorch), ('ONNX', model_onnx)]:
        # Single text test
        metrics = benchmark_single(model, test_sentences[0], backend_name)
        results.append({...})

        # Batch tests
        for batch_size in BATCH_SIZES:
            metrics = benchmark_batch(model, test_sentences, batch_size, backend_name)
            results.append({...})

    print_markdown_table(results)   # Clean GitHub-pasteable output
    print_hardware_info()           # Reproducibility context

if __name__ == '__main__':   # Only run if called directly (not imported as a module)
    main()
```

---

## Errors I Hit and How I Fixed Them

### ERROR 1: `fatal: not a git repository`

**Cause:** Ran `git log --oneline -3` from the home directory `~`, not inside the repo.

**Fix:** `cd ~/Open\ Source/Waggle-mcp` first, then run git commands.

**Lesson:** Always run `pwd` to confirm your location before any git command. Takes two seconds, saves five minutes of confusion.

---

### ERROR 2: UNC Path Blocked (Windows Agent)

**Cause:** The AI agent running on Windows tried to access WSL paths via UNC (`//wsl.localhost/...`) which was blocked by security settings.

**Fix:** Used `wsl bash -c ...` commands to run everything properly inside WSL.

**Lesson:** WSL paths must be accessed via `wsl` commands, not Windows UNC paths. Don't fight the filesystem.

---

### ERROR 3: PR Accidentally Closed

**Cause:** Navigated to the wrong URL (fork PR/2 instead of upstream PR/276). This resulted in the PR being closed — "Closed with unmerged commits."

**Fix:** Navigated to `github.com/Abhigyan-Shekhar/Waggle-mcp/pull/276` and clicked **"Reopen pull request"**.

**Lesson:** Always use the **upstream** repo URL for PR actions, not your fork URL. The merge is done by the maintainer — as a contributor, you should never click merge.

---

### ERROR 4: `pip install` in the Wrong Environment

**Cause:** Packages were installing to system Python instead of `.venv` because the virtual environment wasn't activated.

**Fix:** Ran `source .venv/bin/activate` and confirmed `which python` showed the `.venv` path.

**Lesson:** After every new terminal session, always activate the venv before running pip. The `(venv)` prefix in the prompt is your confirmation.

---

### ERROR 5: Co-authored-by in Commit (Minor)

**Cause:** The AI agent automatically added `Co-authored-by: Copilot...` to the commit message.

**Fix:** Amended the commit to use the proper conventional commit format.

**Lesson:** Commit message format: `type(scope): short description` — keep it clean and intentional.

---

## The Bug CodeRabbit Found — P95 Off-by-One

After the PR was submitted, CodeRabbit (an automated AI code reviewer) flagged the same bug in **two places** — `benchmark_single()` at line 153 and `benchmark_batch()` at line 205.

The comment said: *"P95 calculation is off by one position."*

### The Math Problem

For a list of 100 latency values:

| Index | Position | Meaning | Correct for P95? |
|-------|----------|---------|-----------------|
| `list[94]` | 95th element | True 95th percentile | ✅ YES |
| `list[95]` | 96th element | P96, not P95 | ❌ NO — this was the bug |

### Before (Wrong)

```python
# WRONG — off by one
p95_latency = sorted(latencies)[int(0.95 * len(latencies))]
# For n=100:
# int(0.95 * 100) = int(95) = 95
# list[95] = 96th value = P96!
```

### After (Fixed)

```python
# CORRECT
p95_latency = sorted(latencies)[int(0.95 * (len(latencies) - 1))]
# For n=100:
# int(0.95 * 99) = int(94.05) = 94
# list[94] = 95th value = TRUE P95 ✓
```

### How I Fixed It

Used `grep` to find both occurrences, then `sed` to fix them in one shot:

```bash
# Find both occurrences
grep -n '0.95 \* len' scripts/benchmark_embed.py
# Output: Line 153 and Line 205

# Fix using sed (both at once)
sed -i 's/int(0\.95 \* len(latencies))/int(0.95 * (len(latencies) - 1))/g' \
    scripts/benchmark_embed.py

# Verify the fix applied correctly
grep -n '0.95 \*' scripts/benchmark_embed.py
# Both lines now show: int(0.95 * (len(latencies) - 1))
```

Then committed and pushed on the same branch — no new PR needed:

```bash
git add scripts/benchmark_embed.py
git commit -m 'fix(embeddings): correct P95 percentile index calculation in benchmark'
git push origin perf/benchmark-embed-latency-pytorch-vs-onnx
```

After pushing, CodeRabbit's original comments were automatically marked **"Outdated"** — meaning the bot detected the fix was applied. The issue was closed.

---

## Git and GitHub Workflow

### Full Contribution Flow

| Step | Action | Details |
|------|--------|---------|
| Step 1 | Fork the repo | Created `amankumarhappy/Waggle-mcp` from `Abhigyan-Shekhar/Waggle-mcp` |
| Step 2 | Clone fork locally | `git clone ...` — repo on WSL Ubuntu |
| Step 3 | Comment on issue | Said: *"Hi @ard12, I'd like to work on this..."* with a clear plan |
| Step 4 | Get assigned | Maintainer @ard12 assigned the issue to amankumarhappy |
| Step 5 | Create branch | `git checkout -b perf/benchmark-embed-latency-pytorch-vs-onnx` |
| Step 6 | Write the script | Created `scripts/benchmark_embed.py` — 381 lines |
| Step 7 | Lint check | `ruff check scripts/benchmark_embed.py` — zero errors |
| Step 8 | Format check | `ruff format --check` — no changes needed |
| Step 9 | Commit | `git commit -m 'perf(embeddings): add benchmark script...'` |
| Step 10 | Push | `git push origin perf/benchmark-embed-latency-pytorch-vs-onnx` |
| Step 11 | Open PR | GitHub showed "Compare & Pull Request" button |
| Step 12 | Write PR description | What I built, how to run, results table, hardware info, checklist |
| Step 13 | Comment on issue | *"Hi @ard12, I've opened the PR..."* |
| Step 14 | CodeRabbit review | Bot flagged P95 bug in 2 places |
| Step 15 | Fix the bug | New commit on same branch — sed fix + push |
| Step 16 | CodeRabbit outdated | Bot detected fix — comments marked outdated |
| Step 17 | Maintainer approval | @Abhigyan-Shekhar approved + added `gssoc:approved` label |
| Step 18 | PR ready to merge | All checks green, approved — waiting for maintainer to merge |

### Conventional Commit Format

Every commit followed the format used by the Waggle maintainer:

```bash
type(scope): short description

# Examples from this contribution:
perf(embeddings): add benchmark script comparing SentenceTransformer vs ONNX Runtime
fix(embeddings): correct P95 percentile index calculation in benchmark
```

| Type | Meaning |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `perf` | Performance improvement |
| `docs` | Documentation |
| `test` | Test addition |
| `chore` | Maintenance |

---

## PR Lifecycle

| When | Event | Status | Details |
|------|-------|--------|---------|
| Day 1 | PR #276 Opened | 🟡 OPEN | 381 lines added. Initial script with benchmark logic. |
| Day 1 | CodeRabbit review | 🔴 FLAGGED | 2 actionable comments — P95 off-by-one in both functions. |
| Day 1 | Bug fix commit | 🔁 PUSHED | `fix(embeddings): correct P95 percentile index.` Same branch. |
| Day 1 | PR accidentally closed | 🔴 CLOSED | Wrong URL clicked — PR closed without merge. |
| Day 1 | PR reopened | 🟡 OPEN | Navigated to correct URL, clicked "Reopen pull request." |
| Day 5 | Maintainer approval | ✅ APPROVED | @Abhigyan-Shekhar approved. `gssoc:approved` + `SSoC26` labels added. |
| Day 5 | All checks passing | ✅ GREEN | 10/10 CI checks passed. |
| Day 5 | Waiting for merge | ⏳ PENDING | Contributor cannot merge — only maintainer can. |

> **Key Rule:** A contributor **never** merges their own PR. Only maintainers with write access can merge. After approval, the right move is to wait — or post a polite nudge: *"@maintainer the PR is ready whenever you are."*

---

## Benchmark Results

### Hardware

| Component | Details |
|-----------|---------|
| CPU | Intel Core i3 11th Gen |
| RAM | 8GB |
| OS | WSL2 Ubuntu on Windows |
| Python | 3.x (from .venv) |
| GPU | None — CPU only |

### Results Table

| Backend | Mode | Batch | Median (ms) | P95 (ms) | Throughput (txt/s) | Mem (MB) |
|---------|------|-------|------------|----------|-------------------|----------|
| PyTorch | single | 1 | 21.34 | 24.96 | 46.86 | 0.07 |
| PyTorch | batch | 1 | 23.10 | 44.29 | 43.28 | 0.04 |
| PyTorch | batch | 8 | 43.52 | 55.05 | 183.82 | 0.05 |
| PyTorch | batch | 32 | 104.98 | 114.97 | 304.81 | 0.10 |
| PyTorch | batch | 64 | 204.25 | 218.29 | 313.34 | 0.17 |
| ONNX | single | 1 | 6.29 | 8.71 | 159.08 | 0.07 |
| ONNX | batch | 1 | 6.96 | 8.37 | 143.64 | 0.04 |
| ONNX | batch | 8 | 27.22 | 33.16 | 293.92 | 0.05 |
| ONNX | batch | 32 | 84.84 | 91.59 | 377.19 | 0.10 |
| ONNX | batch | 64 | 180.57 | 211.71 | 354.43 | 0.17 |

### Key Observations

- **ONNX is 3.5x faster for single-text** (6.29ms vs 21.34ms) — the biggest win is for real-time API use cases where you're embedding one query at a time.
- **The gap narrows at larger batch sizes** — ONNX is only ~1.13x faster at batch=64. The relative advantage shrinks as batching becomes the dominant cost.
- **Memory usage is nearly identical** for both backends across all tests.
- **For Waggle's typical use case** (single query per request), ONNX is the clear better default.
- These results are from a budget i3 laptop — production servers will show even better absolute numbers.

---

## Linux Commands Reference

### Navigation and File Reading

| Command | What It Does | Example Output |
|---------|-------------|----------------|
| `pwd` | Print current directory | `/home/aman_kumar_happy/Open Source/Waggle-mcp` |
| `ls` | List files in current folder | `src/ scripts/ tests/ pyproject.toml` |
| `ls -la` | List all files with details | Includes hidden files, permissions, size |
| `cd ~/Open\ Source/Waggle-mcp` | Go to repo folder | `(venv)` prompt stays if venv is active |
| `cat file.py` | Print entire file | All lines in terminal |
| `sed -n '150,196p' file.py` | Print specific lines 150–196 | Just those lines |
| `grep -n 'keyword' file.py` | Find keyword with line numbers | `Line 153: p95_latency = ...` |
| `head -30 file.py` | First 30 lines only | Quick file preview |
| `wc -l file.py` | Count total lines | `381 scripts/benchmark_embed.py` |

### Python and Virtual Environment

| Command | What It Does |
|---------|-------------|
| `python3 -m venv venv` | Create new virtual environment |
| `source .venv/bin/activate` | Activate the venv — see `(venv)` in prompt |
| `which python` | Confirm which Python is active — must be `.venv/bin/python` |
| `pip install -e '.[dev]'` | Install project in editable mode with dev dependencies |
| `pip install sentence-transformers onnxruntime optimum psutil` | Install benchmark dependencies |
| `python -m py_compile script.py` | Check for syntax errors without running |
| `python scripts/benchmark_embed.py` | Run the benchmark |
| `python3 -c 'import x; print(x.__version__)'` | Quick test if a package is installed |

### Git Commands

| Command | What It Does | When to Use |
|---------|-------------|-------------|
| `git status` | Show what changed | Before every commit |
| `git log --oneline -3` | Last 3 commits (short) | Confirm commit happened |
| `git remote -v` | Show remote URLs | Confirm fork is the correct remote |
| `git checkout -b branch-name` | Create and switch to new branch | Start of every task |
| `git add scripts/file.py` | Stage specific file | Stage only what you changed |
| `git commit -m 'message'` | Save changes with message | After staging |
| `git push origin branch-name` | Send to GitHub | After committing |
| `git commit --amend -F file.txt` | Fix last commit message | Immediately after a wrong commit |

### Ruff — Code Quality

| Command | What It Does |
|---------|-------------|
| `ruff check scripts/benchmark_embed.py` | Check for linting errors — must be zero |
| `ruff format scripts/benchmark_embed.py` | Auto-format the file |
| `ruff format --check scripts/benchmark_embed.py` | Check formatting without changing file |
| `pip install ruff` | Install ruff if not present |

---

## Lessons Learned

These are the 14 lessons I'm carrying forward from this contribution.

**L01 — Read the source code before writing anything**
Before writing `benchmark_embed.py`, I read `embeddings.py` thoroughly. That's how I discovered `batch_size = min(64, ...)` — which explained exactly why the issue wanted batch sizes `[1, 8, 32, 64]`. Random guessing would have produced the wrong tests. The codebase tells you what to build.

**L02 — `pwd` and `which python` — run these first, always**
The "not a git repository" error happened because I ran git from the wrong directory. `pwd` would've caught it instantly. `which python` confirms the venv is actually active. Two commands, two seconds, zero wasted time.

**L03 — Virtual environment = your project's clean room**
Installing packages globally breaks other projects and creates version conflicts. Every project gets its own venv. Never install to system Python for project work. The `(venv)` prefix is your confirmation it's active.

**L04 — Warmup runs are mandatory in any performance benchmark**
The first few runs of an ML model are slow because CPU caches are cold. Including them skews results. 5 warmup iterations discarded = much more accurate measurements.

**L05 — Use median, not average, for latency metrics**
A single slow run (network hiccup, background process) wrecks an average. Median is the middle value — robust to outliers. `statistics.median()` is the right tool. Always report median and P95, never just average.

**L06 — P95 = worst realistic case, not worst possible case**
P99 and P100 can be extreme outliers. P95 means 95% of requests were faster than this value. This is what production teams use for SLA definitions. The correct formula: `sorted(latencies)[int(0.95 * (len - 1))]`.

**L07 — `gc.collect()` before measurement = fair baseline**
Python's garbage collector runs at unpredictable times. Forcing it before measurement ensures both backends start with clean memory — not one looking slower because GC happened to run during its test.

**L08 — `tracemalloc` tracks Python memory precisely — use it over `psutil`**
`psutil` measures OS-level process memory, which includes interpreter overhead, loaded libraries, and unrelated allocations. `tracemalloc` tracks only what your Python code allocates — much more precise for comparing two approaches.

**L09 — Conventional commits — use them on every project**
`type(scope): description` — this format tells reviewers instantly what changed. `perf(embeddings): add benchmark...` and `fix(embeddings): correct P95...` are immediately understandable. The maintainer used this format too.

**L10 — Ruff check before every commit — non-negotiable**
@ard12 explicitly wrote in the issue: "PRs get sent back when ruff fails." One missed lint check = PR rejected = wasted time. Pre-commit checklist: `ruff check → ruff format → git add → git commit → git push`.

**L11 — PR number and Issue number are always different**
Issue #123 = the problem statement. PR #276 = the solution. GitHub has separate counters for issues and pull requests. Don't panic when they don't match — they never will.

**L12 — Same branch push = PR auto-update (no new PR needed)**
When CodeRabbit flagged the P95 bug, I didn't need a new PR. I fixed it, committed to the **same branch**, and pushed. PR #276 automatically picked up the new commit and CodeRabbit's comments became "Outdated."

**L13 — Contributors don't merge — maintainers do**
After approval, there's no merge button for contributors. Only people with write access can merge. After approval, the contributor's job is done. Wait, or post a polite comment.

**L14 — Reply to code review comments — it shows professionalism**
When CodeRabbit flagged the bug, replying with "Fixed in follow-up commit" and clicking "Resolve conversation" signals to the community that you're responsive. Maintainers notice contributors who engage vs those who just push and disappear.

---

## Final Checklist

- [x] Issue #123 claimed — first to comment, assigned by @ard12
- [x] Codebase read — `embeddings.py` lines 1–340 studied before writing any code
- [x] `scripts/benchmark_embed.py` created — 381 lines, production quality
- [x] Both backends tested — PyTorch and ONNX Runtime
- [x] All 4 batch sizes covered — 1, 8, 32, 64
- [x] All 4 metrics reported — median, P95, throughput, memory
- [x] Warmup runs implemented — 5 iterations discarded
- [x] Randomness seeded — `RANDOM_SEED = 42`
- [x] `ruff check` passed — zero linting errors
- [x] `ruff format` passed — no formatting changes needed
- [x] PR #276 submitted — 2 commits, detailed description
- [x] P95 bug fixed — CodeRabbit flagged, fixed same day
- [x] PR reopened after accidental close
- [x] PR approved by @Abhigyan-Shekhar — `gssoc:approved` label added
- [x] All 10 CI checks passing

---

This contribution proves that you can read unfamiliar code, write production-quality Python, fix bugs under code review, navigate Git/GitHub professionally, and contribute to a real open-source project used by real teams.

---

*Aman Kumar Happy | GSSoC 2026 | Waggle-mcp PR #276*
*Founder, Mediokart | B.Tech CSE 2025–2029, GEC Buxar*
