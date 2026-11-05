# Usefulness and limitations

Assignment item 6: the usefulness of the agent in real-life settings and its limitations,
with concrete examples. Every limitation below names a case where the agent gives a wrong or
unreliable verdict.

Every report also carries a short `usefulness_limitations` block (4 + 4 items,
`core/usefulness.py`). That block is kept word for word from the Node version, because the
parity check compares reports byte for byte, so it still mentions the Node command names. This
document is the full, current version for the final report.

---

## Usefulness

### U1. Grading a whole lab course unattended

**Example.** 150 students submit `sort.c` for CS5103 in the last hour before the deadline.
The API only stores each file and queues a job; two workers with three sandboxes each grade
them with the same rules, the same tests and the same seeds. The live log shows each student
their progress, and at the end the instructor downloads `gradebook.csv`. TAs open only the
reports that carry a flag.

**Benefit.** Hours of compiling and running by hand become minutes, and two students with
the same mistake always lose the same marks. A crashed worker does not lose work: its jobs are
requeued after 60 s.

### U2. Instant, specific feedback before the deadline

**Example.** A student uploads `add.c` and gets, within a minute:
`INPUT 2147483647 1, EXPECTED 2147483648, GOT -2147483648`, with UBSan's
"signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'" and the advice to
use `long long`. For `sort_integers` a failing random array is shrunk to a few elements that
still fail, so the counterexample is small enough to trace by hand.

**Benefit.** "Wrong answer" becomes a failing input, the expected output and a reason, which is
how students actually learn to debug. They can fix it and resubmit (up to 20 times an hour).

### U3. Plagiarism triage across the class

**Example.** On the seed class, the scan pairs `add_good.c` with `add_peer_copy.c` at
similarity 1.0 (two matched regions, AST similarity 1.0) and `add_web_copy.c` with the forum
solution `web:forum_safe_add.c` at 1.0 (lines 6-29 against 6-23), even though variables were
renamed. The instructor opens the side-by-side view with the matched lines highlighted and
confirms or dismisses each flag.

**Benefit.** Manual checking is limited to a few clusters instead of all pairs, and renaming or
reformatting does not hide copying. The decision stays with a human, and confirmed flags are
recorded with the reviewer's note.

### U4. Ranking and showing the best solutions

**Example.** After the deadline the leaderboard ranks only students who pass every hidden test
and have no confirmed copy flag. At equal grades a faster program ranks higher by measured
instructions: at n = 8000, `sort_good.c` needs 14.9 M instructions and `sort_bubble.c` 348 M.
With the instructor's permission the top three solutions are shown to the class, anonymised.

**Benefit.** Students see what a good O(n log n) solution looks like, and the ranking can be
explained (grade, then speed, then memory, then readability, then length), and is repeatable.
The same ranking works for hiring screens with hundreds of candidates.

---

## Limitations

### L1. Tests cannot prove correctness

**Wrong verdict.** A submission contains
`if (a == 123457 && b == -9) puts("0"); else printf("%lld\n", (long long)a + b);`.
No hidden test, edge case or random trial uses exactly that pair, so the agent reports full
correctness and full logic marks for a program that is wrong on one input.

**Why.** Testing can show the presence of bugs, never their absence. The optional LLM pre-read
may notice such a branch and suggest the input, but only if the model happens to see it.

**Mitigation.** Generators that also try constants read from the source; more trials; treat
100 % as "no failure found", not "proven correct".

### L2. AI-origin false positive on a careful student

**Wrong verdict.** A careful student writes `sort.c` with 4-space indentation, spaces after
every comma and around every operator, braces on every `if`, `int main(void)`, `//` comments
above each function, descriptive names and `return 0;`. Every stylometry feature looks
"machine-like", so `ai_detector` reports band **elevated** ("some signs of AI-generated
code"). If the student's merge sort is also close to one of our generated merge sorts
(containment >= 0.85 on at least 15 fingerprints), the band becomes **high** and the
`possible_ai_generated` flag is raised for a student who wrote every line themselves.

The seed data shows how close this is: the student-style `add.c` matches two generated programs
in `corpus/llm` at 0.875. Only the low fingerprint count (8-9, below 15) keeps it at
"uncertain".

**Why.** Style is not authorship: good habits taught in class are the same habits the model
learned. Short or standard solutions converge.

**Mitigation.** Weight 0 and `penalize: false` by default; "high" requires a strong signal; the
band is a range with evidence and a disclaimer; staff only; it is a reason to talk to the
student, never a verdict.

### L3. Originality depends on submission order until "Finalize"

**Wrong verdict.** Student A submits an original solution on Monday. At that moment there are
no other submissions, so A's originality is 1.0 with no flag. On Wednesday student B submits
A's code with renamed variables; B is compared with A and gets `suspected_copy_peer` and 0 for
originality. A's stored result still shows no flag. Had B submitted first, *A* would have been
the one flagged. The same submission can also change score as the class grows, because
fingerprints shared by half the class are ignored as boilerplate, and that set moves.

**Why.** In class mode the peer corpus is "the latest submission of every other student at
grading time", so results before the deadline are a function of who came first. Each result
stores its `corpus_hash`, so the difference is visible, but the grade differs.

**Mitigation.** Treat pre-deadline originality as provisional; **Finalize** regrades every
student's chosen submission against one frozen peer set, and the pairwise plagiarism scan is
symmetric (it reports the pair, not who copied whom).

### L4. LLM readability can vary between machines and GPU offload splits

**Wrong verdict.** On the laptop (GTX 1650, 4 GB), the 4.7 GB model does not fit in VRAM, so
Ollama decides at load time how many layers go to the GPU, based on the free VRAM (a browser or
Docker Desktop using the GPU changes it). GPU and CPU kernels round floating point
differently, so even at temperature 0 with seed 42 the most likely token can flip somewhere in
the reply. A file reviewed as 8/10 in one session can come back as 7/10 in another, and the
grade moves from 82.3 to 81.3. The lab server with a larger GPU can disagree with the laptop in
the same way, as can a different Ollama version or model build.

**Why.** "Deterministic" sampling is only deterministic for identical arithmetic.

**Mitigation.** Every reply is cached by a hash of the exact prompt and parameters, so a
repeated grade on the same setup is identical; ship `data/cache/llm` with the grades; pin the
model in the rules; readability is only 10 % of the grade and the LLM never touches
correctness.

### L5. WSL isolation is weaker than Docker, and its tool set differs

**Wrong verdict (safety).** On a Windows laptop without Docker and without `ag-run` installed
("weak" WSL), a submission doing `fopen("/mnt/c/Users/<name>/Desktop/notes.txt", "w")` runs as
the normal WSL user, with network access and with the Windows drive mounted. The denylist
classes a write-mode `fopen` as a warning, not critical, so the block policy does not stop it.
Even strong WSL (`ag-run`) has namespaces and an unprivileged user but no memory cgroup and no
read-only root.

**Wrong verdict (grading).** Without `sandbox/wsl_setup.sh`, WSL has no cppcheck or valgrind.
`static_analysis` then finds no cppcheck defects, so `add_crash.c` gets structure 1.0 on WSL
but 0.7 under Docker; complexity falls back to wall-clock timing, which is noisy and not
reproducible.

**Mitigation.** Use Docker for real grading (the parity check requires it); `tool_versions`
records `"sandbox": "wsl (weak)"`; critical denylist findings block the exec analyzers on every
sandbox other than Docker; run `wsl_setup.sh` to get the same tools.

### L6. Measured complexity depends on the generated inputs

**Wrong verdict.** A quicksort that always takes the first element as the pivot is O(n log n)
on the random arrays used for measurement, so the estimator reports O(n log n) and full marks,
although it is O(n^2) on already sorted input.

**Mitigation.** Measure several input shapes (sorted, reverse, all equal) and grade the worst;
the cost is longer grading time.

### L7. Output checking is literal

**Wrong verdict.** A program that computes the right sum but prints `Sum = 12` instead of `12`
fails every hidden test and scores 0 for correctness, and the edge-case runner reports every
case as failing.

**Mitigation.** Only trailing spaces, trailing newlines and CRLF are normalised; state the
exact output format in the problem statement and show it in the sample output.

### L8. Similarity is unreliable for very short programs

**Wrong verdict.** On `add_two_integers`, `add.c` and `add_injection.c` have similarity 1.0, but
after boilerplate is removed only 8 fingerprints are left, so the agent reports
"low confidence, not penalised". A genuine copy of a 6-line program is therefore never
penalised, and two students who independently write the same short code get the same
low-confidence flag.

**Mitigation.** Originality is a triage flag for a human, never proof; it carries real weight
only on longer problems.

### L9. Codacy's C rules are pattern matches and miss real bugs

**Wrong verdict.** With the rules `codacy-cli init` enables by default, our test file `bad.c`
(`gets(buf); strcpy(d, buf); sprintf(d, "%s", buf); system(buf); printf(buf);` in one `main`, plus
a function with cyclomatic complexity 23) got only two findings: `gets` and the complexity. It
missed `strcpy`, `sprintf`, `system(buf)` and the format-string bug `printf(buf)`. After we enabled
25 of Codacy's own C rules in the template, all five calls are reported, but the rules are still
patterns, not analysis of values:

* `seeds/problems/sort_integers/submissions/sort_heap_overflow.c` writes `numbers[n] = INT_MAX`
  one element past a `malloc(sizeof(int) * n)` buffer (line 16). Codacy reports **nothing** for
  this file, so `codacy_review` gives it 1.0. ASan in `sanitizer_run` catches the overflow.
* `char name[8]; scanf("%s", name);` (no field width) and `a[4] = 1` on a 4-element heap array
  also produce no Codacy finding.
* The `strcpy` rule fires on every `strcpy`, including safe copies into a large enough buffer,
  so a careful student can still lose 0.08 of the metric.

**Mitigation.** `codacy_review` carries a small weight (0.05 in the class rules) and caps its
penalty at 0.6; memory safety is judged by executing the program under ASan/UBSan and valgrind,
not by Codacy. Codacy findings are mainly evidence for the student and context for the LLM.
