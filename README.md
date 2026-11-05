# AutoGrader

A rule-driven grader for C programs. CS5103 Agentic AI lab, IIT Patna.

Group: Akash Prasad (2611AI08), Gourav Rajak (2611AI07), Bibaswan Das (2611MC16),
Shakti Prasad Nahak (2611AI46).

A student uploads a `.c` file. The agent compiles and runs it safely, checks it against
the instructor's `rules.json`, and returns one grade out of 100 with feedback. The same
file with the same rules always gets the same grade.

## What it does (mapped to the assignment)

| Requirement | How we do it |
|---|---|
| Run safely with time and memory limits | Every run happens in a throwaway Docker container (`autograder-sandbox:1`) with no network, `timeout`, `prlimit` and a pids limit. Infinite loops, crashes and fork bombs are stopped. |
| Rules kept outside the code | `rules.json` per problem: metrics, analyzer, weight, thresholds. Edit it and regrade, no code change. |
| One analyzer per metric | 11 analyzers in `backend/autograder/core/analyzers/`. The coordinator runs them in parallel and adds `score x weight`. |
| Failing input with expected output | Hidden tests, named edge cases and random tests are compared with the instructor's reference solution. The report shows the input, the expected output, what the program printed and why. |
| Memory leaks and profiling | AddressSanitizer, UBSan and valgrind memcheck; valgrind callgrind counts instructions to estimate the complexity. |
| Copy detection | Winnowing fingerprints (the method used by MOSS) plus an AST comparison, against classmates, saved web solutions, AI-generated solutions and the reference solution. |
| Usefulness and limitations | In every report and in `docs/USEFULNESS_LIMITATIONS.md`. |

The LLM runs locally with Ollama (`qwen2.5-coder:7b`, temperature 0, seed 42, answers
cached). It scores readability and writes advice. It never decides correctness.

## Start

Needed: Python 3.11, Docker Desktop, Node/npm (only to build the web page), and Ollama (optional).

```powershell
.\start.ps1
```

Open **http://127.0.0.1:8020**. On a new database the launcher creates the admin account
and prints its password. Students get accounts from the admin (roster CSV import).

First-time install on a new computer:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
cd frontend; npm ci; npm run build; cd ..
docker build -t autograder-sandbox:1 sandbox
ollama pull qwen2.5-coder:7b          # optional
```

## Command line

```powershell
.\.venv\Scripts\python -m autograder.cli grade add_two_integers seeds\problems\add_two_integers\submissions\add.c
.\.venv\Scripts\python -m autograder.cli batch all
.\.venv\Scripts\python -m autograder.cli validate seeds\problems\add_two_integers\rules.json
.\.venv\Scripts\python -m autograder.cli web-corpus prime_number --url https://www.geeksforgeeks.org/...
.\.venv\Scripts\python -m pytest
```

Reports are written as `.json` (for machines) and `.txt` (for the student).

## rules.json

```json
{
  "problem": "add_two_integers",
  "time_limit_s": 1,
  "memory_limit_mb": 64,
  "reference_solution": "reference.c",
  "metrics": [
    { "name": "correctness", "analyzer": "io_tests", "weight": 0.20,
      "params": { "testset_dir": "tests/" } },
    { "name": "logical", "analyzer": "edge_case_runner", "weight": 0.10,
      "params": { "cases": ["negatives", "zero", "int_overflow"] } },
    { "name": "originality", "analyzer": "similarity_flag", "weight": 0.10,
      "params": { "corpus_dir": "submissions/", "flag_threshold": 0.85 } }
  ]
}
```

Weights must add up to 1.0; `validate` checks this. Full examples are in `seeds/problems/`.

## How the grade is made

1. **First pass.** Compile with `gcc -O2 -Wall -Wextra -std=c11`, run the sample input, and check that
   `main`, input and output exist. If it does not compile, the quality metrics get 0. If it
   hangs, the run-based analyzers are skipped and the report is marked partial.
2. **Analyzers.** Each one returns a score from 0 to 1, its evidence and a feedback line.
3. **Grade.** `sum(score x weight x 100)`, rounded to one decimal. Metrics marked
   "review only" give no points and the other weights are scaled up to 100.
4. **Report.** The failing inputs, the suggestions, the copy flags, and the usefulness and limitations.

The reference solution runs in its own container, so a student program cannot change it.

## Copy detection

- **Classmates:** every pair of submissions for a problem is compared (Staff → Plagiarism).
- **Websites:** search in a browser, paste the page URLs in Problem → Corpus → Find web
  solutions. The C programs on those pages are compiled and saved in `corpus/web/`.
  Automatic search works if a Google or Brave search API key is put in `.env`.
- **AI tools:** Problem → Corpus → Generate AI solutions asks the local model to solve the
  problem a few times and saves the answers in `corpus/llm/`.
- **Reference solution:** a copy of the instructor's solution is flagged.

A confident match gives 0 for originality and raises a flag for the teacher to review.
A weaker match is only flagged, not deducted.

## Folders

```
backend/autograder/
  core/coordinator.py     the agent: first pass, run analyzers, add up the grade
  core/analyzers/         one file per metric
  core/sandbox/           Docker / WSL sandbox
  core/plagiarism/        class-wide pair scan
  core/webcorpus.py       save C programs from web pages
  core/llm.py             Ollama calls with a cache
  api/                    FastAPI routes for the web app
  worker/                 background grading jobs
  cli.py                  command line
backend/tests/            pytest tests
frontend/src/             React web app (student and staff pages)
sandbox/Dockerfile        the grading container
seeds/problems/           example problems, rules and sample submissions
docs/                     report, video script, how it works, limitations
```
