# AutoGrader: 15-minute video script for four members

Replace the four name placeholders. Each member has exactly **3 minutes 45 seconds**.
The time ranges include speaking, clicking, and short screen demonstrations.
Rehearse at roughly 125 words per minute. Read only the **SAY** passages aloud.
Use the **SHOW** and **PAUSE** instructions as production cues.
Use any remaining seconds to show the screen; start each subsection at its stated timestamp.

## Recording preparation

- Open the improved app at **http://127.0.0.1:8020**. Use this address consistently.
- Start Docker and the app before recording. Start Ollama if demonstrating AI feedback.
- Use separate browser profiles for Admin and Student so switching roles does not end a session.
- Keep passwords, session tokens, and personal account details out of the recording.
- Pre-open the staff Problems page, Queue, and reports **1, 2, and 3**.
- These report numbers currently mean: addition **100/100**, descending sort **65/100**,
  and prime checking **90/100**. Verify these values immediately before filming.
- Use the existing completed runs. Say that they were prepared before recording.
  Do not present an old report as the instantaneous result of a new upload.
- Prepare one simple architecture slide and one clearly labelled **Proposed improvements** slide.
- Keep `release/autograder-lab.zip`, `compose.yaml`, and `docker/RUN-ME.txt` ready to show.
  The current native app already occupies port 8020, so show Docker launch commands on
  a slide instead of starting a second deployment on that port during the presentation.
- The portable bundle starts with fresh data. It does not contain the local demo accounts.

| Member | Time | Responsibility |
|---|---|---|
| Member 1 | 00:00-03:45 | Motivation, roles, question setup, input formats |
| Member 2 | 03:45-07:30 | Student flow, grading tools, results and failed inputs |
| Member 3 | 07:30-11:15 | AI feedback, tested edit, similarity and observed limitations |
| Member 4 | 11:15-15:00 | Architecture, Docker delivery, evaluation and future work |

---

## Member 1: Introduction and question setup — 00:00-03:45

### 00:00-00:40 — Introduce the project

**SHOW:** A title slide: “AutoGrader — Tool-assisted assessment and feedback for C programs.”
Show all four team members' names and the course name.

**SAY:**

“Hello everyone. We are [Name 1], [Name 2], [Name 3], and [Name 4]. Our project is AutoGrader, a web application for assessing C programming assignments and giving students evidence-based feedback.

Our goal is to reduce repeated manual work for instructors while helping students understand their results. The application combines actual program execution, compiler diagnostics, memory checks, configurable grading rules, and an optional local AI reviewer.”

**PAUSE:** Hold the title slide for five seconds.

### 00:40-01:30 — Explain the problem and user roles

**SHOW:** Switch to the app. Open the staff overview, then the roster. Point to the role labels.

**SAY:**

“A teacher normally has to compile many submissions, try different inputs, investigate failures, and write feedback. A single final mark does not explain whether a student lost marks for incorrect output, poor structure, or memory errors.

We support three roles. The Admin manages access and staff accounts. A TA manages teaching workflows, including questions and submissions. A Student accesses published problems and their own submission reports. These permissions are checked by the backend as well as reflected in the interface.”

**PAUSE:** Spend eight seconds showing the three role types. Do not display passwords.

### 01:30-02:45 — Show how a question is configured

**SHOW:** Open Problems. Show the existing “prime number” question and its statement, then its rules.
Point to the reference solution, test upload, limits, and metric weights without saving changes.

**SAY:**

“Here is our prime-number question. Staff provide a statement, a reference C program, and test cases. Each input file can have a corresponding expected-output file, such as zero-one dot in and zero-one dot out.

The rules specify execution limits and the grading rubric. Correctness, clean compilation, structure, modularity, memory safety, and readability can have different weights. This makes the assessment depend on the assignment's teaching objectives.

For example, a teacher can give correctness more importance for an algorithm assignment, while another assignment can explicitly assess decomposition into helper functions. Staff should review these settings before publishing because a generic rubric may be too strict for a short program.”

**PAUSE:** Allow fifteen seconds to point out the settings and the total weight indicator.

### 02:45-03:45 — Explain the input-format selector

**SHOW:** Point to “Generated input format,” the clear button, and the uploaded-test area.
Optionally display a small prepared `tests.zip` directory containing two `.in/.out` pairs.

**SAY:**

“The generated-input selector currently offers two integers, an integer array, one text line, and a square integer matrix. These choices describe the automatic generators that we have implemented.

For a custom text format, staff can clear that selector and upload their own tests. Graphs or multiple arrays can therefore be assessed through a defined standard-input format. Automatic edge-case generation and complexity estimation require a matching generator.

The current submission interface expects a complete C program. Function-only submissions need an additional driver. I will now hand over to [Name 2] to show the grading workflow.”

**PAUSE:** Hold the test-file example and switch browser profiles during the final few seconds.

---

## Member 2: Student workflow and measured grading — 03:45-07:30

### 03:45-04:30 — Show the student's submission flow

**SHOW:** In the Student browser profile, open the prime-number statement and source-upload area.
Show `prime.c` in the editor or file preview. Then open existing submission report 3.

**SAY:**

“I am [Name 2]. From the Student view, we open a published question, read the required input and output format, and provide a C source file.

The API stores the submission and creates a grading job. A background worker performs the analysis, allowing the page to show progress while the work runs.

We submitted these examples before recording. I am opening their completed reports so we can examine the full results without waiting for model inference during the video.”

**PAUSE:** Allow eight seconds for the report to load and point to the submission filename.

### 04:30-05:20 — Explain the tools and execution boundary

**SHOW:** A prepared tool slide or the report's metric list. Briefly show Queue for job status.

**SAY:**

“Different tools answer different questions. GCC checks whether the program builds and reports compiler diagnostics. Uploaded tests measure output correctness. Cppcheck and source analysis inspect structure and possible defects. AddressSanitizer, UndefinedBehaviorSanitizer, and Valgrind provide runtime evidence about memory problems and undefined operations.

Submitted programs run inside separate Docker sandbox containers. The execution setup restricts network access and applies resource limits. This reduces the risk of running untrusted student programs, although the complete grading system still needs further security hardening.”

**PAUSE:** Point to three relevant metric rows for approximately ten seconds.

### 05:20-06:15 — Explain the prime-number result

**SHOW:** Report 3. Open Metrics. Point to correctness, modularity, and readability.

**SAY:**

“The prime program passes all fifteen hidden correctness tests, but its overall grade is ninety. This is because the configured rubric assesses additional qualities.

It earns forty out of forty for correctness. It loses seven marks in modularity because all logic is inside main, and three marks in readability because the fixed rubric finds no comments.

Those deductions come from the configured source-based checks. The AI feedback does not decide this grade. Whether these particular requirements are appropriate is a teaching decision, which is why we expose the rubric and the individual contributions.”

**PAUSE:** Give viewers ten seconds to read the three metric rows.

### 06:15-07:30 — Demonstrate a real output failure

**SHOW:** Switch to the Admin browser profile. Open prepared staff report 1, then staff report 2.
Staff can access both students' reports; a Student session cannot. For report 2, show a failed sorting case,
the expected ascending output, the actual descending output, and line 11 in Source.

**SAY:**

“In the Staff view, our addition example scores one hundred under its configured rubric. The sorting example scores sixty-five and passes only two out of ten hidden tests.

Here, the expected output is ascending, while the submitted program prints descending values. The report provides a concrete failing input and both outputs. That is much more actionable than a generic wrong-answer message.

In the source, the merge operation uses greater-than-or-equal when choosing the next element. That comparison explains the descending order. We intentionally included this defect in the sample to demonstrate feedback on an incorrect solution.

Passing the available tests still does not prove correctness for every possible input. [Name 3] will now explain how the AI reviewer uses the measured evidence.”

**PAUSE:** Spend fifteen seconds comparing the outputs and highlighting the source line.

---

## Member 3: AI feedback and similarity review — 07:30-11:15

### 07:30-08:10 — Explain the AI's role

**SHOW:** The “Code issues and AI feedback” panel on sorting report 2.

**SAY:**

“I am [Name 3]. We use a local Ollama model, configured as Qwen two-point-five Coder seven-B, to assist with code feedback.

The source reviewer receives the submitted source, the problem statement, and selected measured results. It returns structured comments and suggested changes.

We treat these suggestions as hypotheses. The application checks quoted source locations, and correctness edits require additional execution evidence before they are shown as tested improvements.”

**PAUSE:** Allow six seconds to point out the AI-suggestion label and quoted source.

### 08:10-09:10 — Show a tested suggested edit

**SHOW:** On report 2, show the comparison issue, proposed ascending-order edit, and
“2 to 10 of 10 tests pass.” Point to the unchanged original grade.

**SAY:**

“For the sorting example, the reviewer identifies the faulty comparison and proposes an ascending-order edit.

The verifier tests the original program and the proposed version in separate containers. For this suggestion, the test count improves from two out of ten to ten out of ten, without a regression on the checked cases.

This check supplies evidence for the recommendation. It does not apply the edit to the student's submission, and the original grade stays at sixty-five. The student remains responsible for reviewing the suggestion and submitting a revised solution.

The verifier has limits on the number and size of tests, so this remains a bounded check.”

**PAUSE:** Hold the measured improvement and proposed edit for ten seconds.

### 09:10-10:00 — Show an incomplete AI review honestly

**SHOW:** Open report 3's AI comments. Show the modularity comment and withheld-suggestion notice.

**SAY:**

“We also observed an incomplete review on the prime program. The feedback job completed, but the model supplied only one metric comment, about modularity.

Its proposed refactor quoted eighteen source lines, while our current validator allows six. That suggestion was withheld. The interface currently describes the rejection too generally; the quote actually matched the source.

This example shows why we distinguish a completed job from a complete, high-quality review. Better retries, coverage checks, and more accurate rejection reasons are still needed.”

**PAUSE:** Leave the relevant comment and notice visible for eight seconds.

### 10:00-11:15 — Explain similarity and the duplicate test

**SHOW:** Show report 1's “Similarity review: Not assessed” row. Then compare prime
submissions 3 and 4 and show their current insufficient-evidence result. Use staff access.

**SAY:**

“Similarity review compares code against available submissions and stored comparison files. An empty corpus is shown as not assessed. AI-origin information is a heuristic signal, without a percentage of authorship.

Our duplicate test exposed a remaining bug. Prime submissions three and four contain identical code, which also matches the reference. Reference filtering removes every distinctive fingerprint, so the current comparator misses the exact duplicate.

The next repair is an independent exact-file comparison before that filtering, followed by token and structural comparisons. Matching code should be highlighted for staff review, because similarity alone cannot establish who copied whom or whether the overlap is legitimate.

[Name 4] will now explain the architecture, packaging, and proposed extensions.”

**PAUSE:** Allow twelve seconds to compare the two files and show the current limitation.

---

## Member 4: Architecture, delivery, evaluation and future work — 11:15-15:00

### 11:15-12:10 — Explain the architecture

**SHOW:** A slide with this flow:

```text
React interface -> FastAPI -> SQLite submissions and job queue
                                  |
                           Background worker
                           /               \
                   Grading tools       Local AI feedback
                           \               /
                         Stored report -> Interface
```

**SAY:**

“I am [Name 4]. Our frontend uses React, and the backend uses FastAPI. SQLite stores accounts, question versions, submissions, jobs, and reports.

The background worker claims queued work, records progress, and coordinates the configured analyzers. Submitted C programs execute in disposable Docker sandboxes. AI feedback and profiling are handled as background work alongside the grading workflow.

The current system is a configured tool workflow with an AI feedback component. The rubric determines which analyzers run. Autonomous model-selected web searching is a proposed extension, rather than an implemented part of this demonstration.”

**PAUSE:** Trace the diagram for ten seconds.

### 12:10-13:00 — Show the portable Docker package

**SHOW:** The ZIP, its launchers and `compose.yaml`. Display the launch commands on a slide.
Optionally show the two built image names using `docker image ls`.

**SAY:**

“We also prepared a portable Docker bundle for another lab computer. It contains the application image, a separate grading-sandbox image, the Compose configuration, and Windows and Linux launchers.

The target computer needs Docker with Linux containers and Compose. Python, Node, and the C tools are included in the images. Docker volumes preserve data after a container restart.

The bundle starts with a fresh database. Optional AI feedback requires a separate initial model download. In this video, we are displaying the prepared package while the local demonstration app remains running.”

**PAUSE:** Hold the package contents and commands for ten seconds.

### 13:00-14:00 — Present validation and practical limits

**SHOW:** A slide containing verified checks and limitations. Open the verification notes if useful.

**SAY:**

“We validated the project through automated tests, browser checks, and real Docker execution. Our isolated Docker test covers account creation, question upload, a student submission, report generation, and persistence after restart.

Practical limitations remain. Generated tests support a small set of grammars. Function-only submissions require a driver, and some tasks require special output checkers. Complexity estimates are based on measured inputs. AI feedback can be incomplete, and similarity evidence requires staff interpretation.

We also identified harness and report-privacy issues that require further hardening before using the system for high-stakes assessment. Our presentation demonstrates the current prototype and its measured behavior.”

**PAUSE:** Give viewers ten seconds to read the validation and limitation lists.

### 14:00-14:40 — Explain proposed retrieval and web tools

**SHOW:** A slide labelled **PROPOSED — NOT YET IMPLEMENTED**:

```text
Known-source index + public web retrieval
                  |
          Candidate code files
                  |
       Hash / token / structural comparison
                  |
       Source links and matching regions
                  |
          AI explanation + staff review
```

**SAY:**

“For website copying, we propose retrieval over stored sources and optional web-search tools. Retrieved code would be compared using hashes, tokens, and structure. Reports would preserve source links and matching regions.

The model could help draft searches and explain verified matches. RAG would retrieve candidates; the comparison tools would provide evidence. A search with no result would remain inconclusive. The first priority is fixing the exact-duplicate gap.”

**PAUSE:** Hold the proposed-workflow slide for five seconds.

### 14:40-15:00 — Close

**SHOW:** A final slide with all four names and “Questions.”

**SAY:**

“AutoGrader brings grading tools and feedback into one inspectable workflow. Students can see where marks were lost, and staff retain control over the rubric and integrity decisions. Thank you for watching. We welcome your questions.”

**PAUSE:** Hold the closing slide until the 15-minute mark.

---

## Presentation accuracy checklist

- Say **passes the available tests**, rather than **proven correct**.
- Say **similarity evidence**, rather than **plagiarism confirmed automatically**.
- Say **AI suggestions are advisory**; readability marks use the fixed rubric.
- Describe web search, RAG integration, and the independent exact-duplicate stage as proposed work.
- Disclose the current duplicate-detection miss and incomplete prime review if showing those examples.
- Describe Docker restrictions as risk reduction, not a guarantee of complete security.
- Do not cite historical Node parity results as acceptance of the current grading policy.
- Do not reset the database, overwrite the rubric, or change student grades just to match this script.
- Check the reports before recording and replace any changed numbers with the actual displayed results.

## Useful answers if questions are asked after the video

**Why did a correct prime program receive 90?**
It passed the correctness tests. The configured rubric deducted 7 marks for modularity and
3 for missing comments. Those requirements are configurable teaching choices.

**Does the AI decide marks?**
The model provides advisory feedback. Execution and the configured source rubric determine marks.
Staff can apply an override or a configured integrity policy through an explicit review workflow.

**Can it handle hard problems?**
It can run uploaded text-based input/output tests for complete C programs. Arbitrary automatic
generators, LeetCode-style function drivers, and special output checkers require further work.

**Why did the identical prime files escape detection?**
Reference filtering removed all distinctive fingerprints. An exact-match stage independent of
that filtering is the next required repair. This video does not claim that repair is implemented.

**Does the current model browse the web?**
The current review client sends structured chat requests. Search tools, fetching, indexing,
and the tool-result loop need explicit application integration.

**Can this replace a teacher?**
It can automate repetitive checks and organize evidence. Staff still choose the rubric,
review ambiguous findings, and make academic-integrity decisions.
