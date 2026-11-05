"""What the evaluation is useful for and where it falls short.

The text is attached to every new report and describes this copy's policy.
"""

USEFULNESS = [
    {
        "use_case": "Grading a large lab course unattended",
        "example": "Students submit C programs through the web UI. The worker evaluates each against the published rules and tests and stores reports for admin review.",
        "benefit": "Automates repeated execution and collects evidence; throughput depends on the available tools and machine.",
    },
    {
        "use_case": "Instant feedback before the deadline",
        "example": "A student uploads add.c in the web UI and is told: input \"2147483647 1\", expected 2147483648, got -2147483648, because the sanitizer reports signed integer overflow; use long long. They fix it before the real submission.",
        "benefit": "The student gets a concrete failing input and the reason, not just 'wrong answer', which is how people actually learn to debug.",
    },
    {
        "use_case": "Coding-interview and hiring screens",
        "example": "A company gives 500 candidates the 'sort integers' task. The agent ranks them by correctness, edge cases (empty input, INT_MIN/INT_MAX), measured complexity and memory safety, with a per-metric explanation for every score.",
        "benefit": "Fast first-round filtering where every rejection can be justified metric by metric.",
    },
    {
        "use_case": "Plagiarism and LLM-copy triage",
        "example": "Two submissions reach 0.93 similarity (threshold 0.85) after variables were renamed, and a third matches a solution in the LLM corpus. The admin reviews those three files instead of all 300.",
        "benefit": "Helps prioritize human review. Similarity neither proves copying nor establishes AI authorship.",
    },
]

LIMITATIONS = [
    {
        "limitation": "Tests cannot prove correctness",
        "example": "A program contains `if (a == 123457 && b == -9) puts(\"0\"); else printf(\"%lld\\n\", (long long)a + b);`. No hidden test, edge case or random trial happens to use exactly that pair, so the agent reports full correctness for a program that is wrong on one input.",
        "mitigation": "More trials and generators that target the program's own constants (read from the source) would raise the chance of hitting such branches, but testing can only show the presence of bugs, never their absence.",
    },
    {
        "limitation": "Measured complexity depends on the inputs generated",
        "example": "An algorithm can behave well on the normal, sorted, reverse and equal-value samples yet be slow on a different adversarial arrangement or at larger sizes.",
        "mitigation": "The array estimator takes the worst observed shape. Review the selected sizes; empirical measurements do not prove a worst-case complexity bound.",
    },
    {
        "limitation": "Similarity is unreliable for very short programs and for restyled LLM code",
        "example": "Two students independently write the same 6-line add.c. Once the boilerplate shared with the reference is removed, too few fingerprints are left, so the agent reports 'low confidence, not flagged': a genuine copy of a short program would also be missed. A student who takes an LLM solution and restructures the loops evades the LLM corpus.",
        "mitigation": "Treat originality as a triage flag for a human, never as proof; enlarge the LLM corpus with several models and prompts.",
    },
    {
        "limitation": "Readability rules are heuristic, and output checking is literal",
        "example": "A fixed rubric can penalize a concise program for missing comments even if a teacher finds it clear. Printing \"Sum = 12\" instead of the specified \"12\" also fails an output test.",
        "mitigation": "Inspect the rubric and use admin overrides when justified. AI advice never changes the readability score. State the exact output format in the question.",
    },
]


def usefulness_limitations() -> dict:
    return {"usefulness": [dict(u) for u in USEFULNESS], "limitations": [dict(x) for x in LIMITATIONS]}
