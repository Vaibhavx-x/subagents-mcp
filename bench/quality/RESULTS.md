# Is the delegated work any good?

`bench/ab/RESULTS.md` measured what delegation **costs**. It could not measure whether the work was any good, and said so: *"23 of 23 graded runs passed. This bench ranks cost and cannot rank quality."*

This one scores the output. Two tasks, each graded mechanically against a key generated from the edit that produced it, run with the tool (**C**) and without it (**A**).

```bash
python bench/quality/inject.py --check      # the key matches the fixture
python bench/quality/run_quality.py --repeats 4
python bench/quality/report.py
```

---

## Defect audit

Twelve defects planted across three modules; a fourth is **clean**, as a control, so precision has something to measure. Scored on **recall** -- a finding counts when it names a line within 2 of a planted one. Graded on line numbers only, never on the description, which is bench defect D2.

> **CEILING: both arms scored 90%+ -- this task was too easy to separate them, and the accuracy comparison measures nothing.**

| arm | accuracy | spread | wall clock | parent in | in/turn | total in |
|---|---|---|---|---|---|---|
| **A - solo** | **100%** | 100-100% | 135.9s | 295,038 | 295,038 | 295,038 |
| **C - delegating** | **100%** | 100-100% | 118.2s | 69,853 | 69,853 | 750,330 |

**Accuracy: identical** -- both arms 100%.

- **Parent input:** 4.22x less for the delegating arm -- ranges do not overlap.
- **Input per turn:** 4.22x less for the delegating arm -- ranges do not overlap.
- **Wall clock:** 1.15x faster for the delegating arm -- not measurable -- the observed ranges overlap.
- **Total tokens: 2.54x** -- delegation spends more overall, because each worker re-reads context the parent already had.

| arm | n | void | timeouts | format failures | precision |
|---|---|---|---|---|---|
| A | 4 | 0 | 0 | 0% | 100% |
| C | 4 | 0 | 0 | 0% | 100% |


---

## Answer-key extraction

Twelve questions. No question names the module its answer is in -- finding the file is part of the task -- and a third require the answer to be derived rather than found. **Two are deliberately cross-module**, which is where delegating by file was predicted to hurt.

> **CEILING: both arms scored 90%+ -- this task was too easy to separate them, and the accuracy comparison measures nothing.**

| arm | accuracy | spread | wall clock | parent in | in/turn | total in |
|---|---|---|---|---|---|---|
| **A - solo** | **100%** | 100-100% | 46.4s | 123,922 | 123,922 | 123,922 |
| **C - delegating** | **100%** | 100-100% | 89.8s | 110,757 | 110,757 | 432,338 |

**Accuracy: identical** -- both arms 100%.

- **Parent input:** 1.12x less for the delegating arm -- not measurable -- the observed ranges overlap.
- **Input per turn:** 1.12x less for the delegating arm -- not measurable -- the observed ranges overlap.
- **Wall clock:** **1.93x SLOWER** for the delegating arm -- ranges do not overlap.
- **Total tokens: 3.49x** -- delegation spends more overall, because each worker re-reads context the parent already had.

| arm | n | void | timeouts | format failures | precision |
|---|---|---|---|---|---|
| A | 4 | 0 | 0 | 0% | 100% |
| C | 4 | 0 | 0 | 0% | 100% |


---

## The predictions, and how they came out

Written into the plan before any code existed, so they could not be adjusted afterwards. `NOTES.md` section 41 is why: the last prediction was wrong, and having written it down first is what turned a disappointing number into a finding.

| prediction | outcome |
|---|---|
| Arm C carries less parent input on both tasks | **Half right.** 4.22x on the audit, ranges apart. On extraction 1.12x with the ranges overlapping -- not measurable. |
| Audit accuracy roughly equal | **Right**, and uninformatively so: both arms 100%. |
| **Extraction accuracy: delegation slightly worse** | **Wrong.** Arm C scored 12/12 every run, including both cross-module questions. |
| Arm C faster on both | **Wrong on extraction**, where it is **1.93x SLOWER** with ranges apart. Four workers pay ~10s of process startup each to answer twelve questions one agent answers in 46s. |

### Why the cross-module questions did not hurt

Two questions were built so their answers live in a module other than the one the question concerns -- a worker given one file should not have been able to answer them. It answered them anyway, because the premise was wrong: **a worker is not confined to its declared reads.** `--add-dir` is additive scope, `--sandbox` covers terminal commands only, and `--print` mode has no permission gate at all.

The evidence is structured, not read out of a transcript:

| extract worker | questions | median input tokens |
|---|---|---|
| `ask-db` | 4 | 82,045 |
| **`ask-execution`** | **4** | **143,194** |
| `ask-worker` | 2 | 53,440 |
| `ask-hashing` | 2 | 56,343 |

`ask-execution` holds both cross-module questions, carries the same question count as `ask-db`, and 1.7x its input. File size does not explain it: in the audit task, where no worker needs another's file, the largest module (`execution.py`, 791 lines) used *fewer* tokens than the smallest (`db.py`, 248).

**The consequence is about the detector, not the experiment.** An undeclared *read* leaves no trace at all -- it changes no mtime and no hash, so taint detection cannot see it, and the only reason it is visible here is that the worker reports its own token usage. So `reads[]` is a scheduling input and a taint baseline, and it is not a description of what the worker actually read (`NOTES.md` section 47).

---

## What these numbers are not

- **`parent in` is cumulative input across turns, not peak context.** Ten turns of 20k and two of 100k both total 200k, and only the second fills a window. `in/turn` sits beside it as the closer proxy for context pressure; neither alone answers "did the parent's context stay clean".
- **Total tokens go up.** The claim is about the parent's window, not about spending less. A reader who discovered that ratio for themselves after adopting this would be right to distrust everything else here.
- **No delta whose ranges overlap is reported as an effect.** At five repeats there is no significance test worth running, but overlap is free to check. Measured twice already: n=3 said 2.06x and n=5 said 1.59x (`NOTES.md` section 35), which is why four repeats buys ranges rather than effects.
- **A zero-scoring run is in these medians.** Only runs that do not measure what we think they do are void -- wrong arm state, a cache hit, agy failing outright. Dropping runs that scored badly would have been the comfortable choice and would have deleted the difference between the arms.
- **Arm C is measured under close-to-best-case delegation.** Its prompt carries the decomposition and tells it not to read the source itself. A parent meeting this server cold spends ~16 tool calls exploring first (`NOTES.md` section 37), and that is not in these numbers.
- **`in/turn` carries no information in this measurement.** It was added because cumulative input is not peak context -- but agy reports a whole `--print` run as **one turn**, in all 16 runs, so the column is identical to `parent in`. The distinction it exists to draw is not observable through this interface. Reported rather than dropped: a column removed because it did not discriminate is a column nobody can check (`NOTES.md` section 46).
- **Scope:** one machine, one model, two fixtures derived from one codebase, 4 repeats per cell, 16 runs, 0 void.

