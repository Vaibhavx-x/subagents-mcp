# A/B — what delegation costs, and what it saves

15 runs, 0 void. Each run asks for the same deliverable: one Markdown document per module for 4 modules (worker, execution, hashing, waves), each at least 300 bytes and naming something the module actually defines.

Three arms, because two would conflate two different costs:

| arm | n | void | parent input (median) | spread | parent output | cache-read | wall s |
|---|---|---|---|---|---|---|---|
| **A · solo** | 5 | 0 | **99,582** | 78,114–144,008 | 7,745 | 224,286 | 49 |
| **B · registered** | 5 | 0 | **83,782** | 65,566–139,062 | 7,444 | 175,292 | 44 |
| **C · delegating** | 5 | 0 | **62,551** | 34,438–66,162 | 1,192 | 12,188 | 50 |

- **A · solo** — server not registered; the parent does everything
- **B · registered** — server registered, parent not told to use it
- **C · delegating** — server registered, parent delegates

## The two numbers the three arms exist to separate

Each difference is reported with whether the arms' observed ranges actually separate. 5 runs per arm supports no meaningful significance test, but overlapping ranges are enough to say a difference is not measurable -- and saying so is the entire reason the spread column exists.

- **Registration toll (B - A): -15,800 parent input tokens** -- *within run-to-run variance -- the observed ranges overlap.*

  Registering the server was expected to COST the parent context: the tool schemas and `instructions.md` load on every turn. This measurement cannot see that cost at this sample size, and the sign came out negative. That is a result about the noise floor, not evidence that registration is free. **Do not quote it as a saving.**

- **Delegation saving (B - C): 21,231 parent input tokens** -- *within run-to-run variance -- the observed ranges overlap.*
- **Net (A - C): 37,031 parent input tokens** -- *ranges do not overlap.* A two-arm test would report only this number and could not say which half of it is registration and which is delegation.
- The delegating parent carried **1.59x less input** than the solo parent.

## What this cost in total tokens

| arm | parent input | worker input | total input |
|---|---|---|---|
| A · solo | 99,582 | 0 | **99,582** |
| B · registered | 83,782 | 0 | **83,782** |
| C · delegating | 62,551 | 170,914 | **233,465** |

**Delegation cost 2.34x the total tokens of doing it alone.** That is the honest shape of the trade and it is not a footnote: four workers each re-read context the parent already held. The claim this project makes is about the **parent's context window**, which is what fills up and forces a compaction — not about total spend, which goes up.

## What this cannot tell you

- **n = 5 per arm.** Bench runs varied 110k–305k input tokens per turn. Three runs cannot separate an effect smaller than the spread column, and the spread is printed for exactly that reason.
- **The arms are not prompt-identical.** A and B receive the same prompt; C's tells it to delegate. That asymmetry *is* the intervention, and it means C is measured doing something the others were never asked to do.
- **All three arms ran with `--dangerously-skip-permissions`.** Without it a headless parent cannot call an MCP tool at all (`NOTES.md` §31). File tools are auto-approved headlessly regardless, so the flag changes nothing for A and B — but no human approval gate was exercised in any of these runs, and that is not the configuration a person running this interactively would have.
- **One fixture, one language, one model** — `gemini-3.8-flash-low` over four Python modules. Inherited from `bench/RESULTS.md` §3 and still true.
- **Cache-read is reported separately** and never folded into input tokens.

## Reproducing

```bash
python bench/ab/run_ab.py --repeats 3   # spends tokens
python bench/ab/report.py
```

`--dry` swaps in `tests/fake_agy.py` and spends nothing; arm C correctly voids there, because the fake speaks no MCP.
