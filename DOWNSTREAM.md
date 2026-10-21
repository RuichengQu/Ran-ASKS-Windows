---
schema: asks-community-downstream-record-v1
name: "Ran-ASKS-Windows"
status: community
upstream_repository: "https://github.com/ranshiju/Ran-ASKS"
upstream_version: "0.16.0"
upstream_commit: "7a864aa337102bfdf05a4fce3385a1ce563352eb"
maintainers:
  - name: "Ruicheng Qu (曲锐诚)"
    contact: "https://github.com/RuichengQu/Ran-ASKS-Windows/issues"
---

# Community Downstream Record

> Community project based on Ran-ASKS. Independently maintained and not an
> official Ran-ASKS distribution.

This record documents a downstream's declared baseline and maintenance
boundary. It is not an extension manifest, compatibility certificate, or
endorsement by the Ran-ASKS maintainer.

## Purpose And Ownership

- **Domain or task:** Two separable concerns share this repository.
  1. **Windows portability of Core.** Making Ran-ASKS run on Windows without
     changing the method, the data contracts, or the frozen paper artifacts.
     This part is domain-independent and is the natural upstream candidate.
  2. **Mathematical Physics Methods course knowledge base.** Compiling a
     university course (courseware, textbooks, exercises) into a traceable
     question-answering knowledge base. This part is domain-specific and
     stays downstream.
- **Repository owner:** Ruicheng Qu, Capital Normal University.
- **Issue tracker:** https://github.com/RuichengQu/Ran-ASKS-Windows/issues
- **Release policy:** Track upstream releases. Upgrade on a short-lived
  branch, keep the previous port on a `backup-<version>-port` branch, and
  merge only after the contract checks and the test comparison below are
  rerun on the new baseline.

## Upstream Baseline

| Field | Value |
| --- | --- |
| Version | **0.16.0** |
| Commit | `7a864aa337102bfdf05a4fce3385a1ce563352eb` |
| Upgraded on | 2026-10-14 |
| Previous baseline | 0.15.0 |

Earlier baselines: v0.6.0 (initial port, branch `backup-v0.6.0-port`),
v0.8.0 (branch `backup-v0.8.0-port`), v0.10.0, v0.15.0, v0.17.0, v0.17.0.
The `upstream` remote points at the Ran-ASKS public tree.

**Every upstream commit on `upstream/main` at v0.17.0 is merged into this
port's `main`.** The port is kept in step with upstream release by release;
it never forks the method. Merges use a real `git merge` so that hand-written
Windows fixes in non-conflicting files are preserved — only conflicting files
are taken from upstream and then re-transformed by the port's codemod.

### Verification on this baseline

Same Windows machine, same command
(`python -X utf8 -m unittest discover -s .scripts -p "test_*.py"`):

| Tree | Tests | Failing |
| --- | --- | --- |
| Clean upstream v0.17.0 | 305 | 115 |
| This port, v0.17.0 | 340 | 52 |

`graph_validate.py`: **0 ERROR** (171 WARN are upstream's own
`missing_semantic_description`, unrelated to portability).

## Domain Capabilities

Maintained only by this downstream, outside Core:

- **Course knowledge base** — 39 topic pages under `teaching/wiki/topics/`
  (14 compiled from lecture slides, 25 from a textbook), and the graph they
  produce: 213 nodes, 310 edges.
- **Courseware ingestion pipeline** — extracting text and speaker notes from
  `.pptx`, locating MathType OLE objects by their resolved group transforms,
  cropping the matching regions out of the PDF rendering, and converting them
  to LaTeX with a vision model. Slides alone carry no readable formulas and
  the exported PDF's text layer is 68% mojibake, so both inputs are required.
- **Scanned-textbook ingestion** — MinerU OCR with `is_ocr=True`, then
  splitting by the book's own sections into topic pages with printed-page
  level sources.
- **Retrieval and evaluation harness** — dual-vector retrieval (raw text and
  Chinese description, similarity taken as the max), local bge-m3 embeddings,
  local Qwen3-8B answering, and a heterogeneous LLM judge.
- **Domain evaluation sets** — see *Domain Evaluations*.

None of this is proposed upstream.

## Core Files Modified

157 files differ from the upstream baseline. Grouped by the reason for the
change rather than listed individually:

| Core path | Reason | Retain downstream or propose upstream | Migration note |
| --- | --- | --- | --- |
| `.scripts/**` (text writes) | Windows text mode translated `\n` to `\r\n`, so `sha256(file on disk)` disagreed with `sha256(content)`; source fingerprints, `require_hash` and `CHECKSUMS.sha256` then contradicted each other. Every text write pins `newline="\n"`. | **Propose upstream** — a no-op on POSIX | Mechanical; replayable by `tools/port_codemod.py` |
| `.scripts/**` (relative paths) | `str(p.relative_to(root))` yields backslashes on Windows, which leaked into `graph.db`, JSON state and locators. Replaced with `.as_posix()`. | **Propose upstream** — a no-op on POSIX | Mechanical; replayable |
| `.scripts/**` (SQLite) | `with sqlite3.connect(...)` manages the transaction, not the handle. Leaked handles block `os.replace` and directory cleanup on Windows. | **Propose upstream** — a latent resource leak on any platform | Hand-written; **not** replayable by codemod |
| `.scripts/**` (subprocess) | Output decoded as UTF-8 explicitly rather than the system locale (cp936 on Chinese Windows); `git check-ignore --stdin` fed bytes so its newline translation stops appending `\r`. | **Propose upstream** | Hand-written |
| `.scripts/platform_compat.py` (new) | Single seam for file locking (`fcntl.flock` / `msvcrt.locking`), interpreter path, macOS-only commands (`textutil`, `open`, `find`, `curl`, `trash`), LibreOffice discovery, `signal.SIGALRM`, directory fsync. | **Propose upstream** | New file; also needs its node entry in `operations/engineering/graph.yaml` |
| `.scripts/read_section.py` (new) | Cross-platform twin of `read_section.sh`, which needs bash. Identical output and exit codes; the `.sh` is left in place. | **Propose upstream** | Also needs a `read_section` entry in the `untracked` whitelist |
| `.scripts/test_*.py` (symlinks) | Windows refuses symlink creation without Developer Mode. Those tests now skip rather than report a failure. | **Propose upstream** | Hand-written |
| `.scripts/**` (`import fitz`) | PyMuPDF ≥ 1.24 prints a deprecation warning to stderr on `import fitz`, and the ingestion pipeline treats a subprocess's stderr as the failure reason — one warning failed every PDF ingestion. | **Propose upstream** | Mechanical; replayable |
| `.gitattributes` (new) | Git for Windows defaults to `core.autocrlf=true`, so a clone rewrote every text file to CRLF and both frozen paper artifacts failed their checksums. `paper-artifacts/**` is marked `-text`. | **Propose upstream** | Content hashes are part of the data contract |
| `.scripts/query_actions.py`, `.scripts/wg.py` (separators) | The `[raw source=…]` citation header and `wg.py read-raw`'s `source_path` / `read_path` / `evidence_locator` were still built with `str(path.relative_to(REPO))`, so every locator a model saw on Windows carried backslashes while every locator it was compared against used forward slashes. The port's separator pass had matched a narrower pattern and missed the `str(X.relative_to(Y))` shape. | **Propose upstream** — a no-op on POSIX | Hand-written; `tools/port_codemod.py` does not yet match this shape |
| `.scripts/query_actions.py` (subprocess) | `graph_query` spawned `query_graph.py` with a bare `subprocess.run(..., text=True)`. The child inherits neither `-X utf8` nor an explicit encoding, so on Chinese Windows it wrote cp936 while the parent decoded UTF-8 and every graph query raised `UnicodeDecodeError`. Routed through the port's own `platform_compat.run_text`. | **Propose upstream** | Three production call sites remain unrouted (`ingest_paper.py`, `recompute_tokens.py`) |
| `.scripts/test_private_query.py` (symlinks) | Four tests create symlinks; Windows refuses without Developer Mode. Now skipped with the port's existing `_pc.symlinks_available()` guard rather than reported as failures. | **Propose upstream** | The checks themselves go untested on Windows — this hides nothing that was passing |
| `dsh/scoring.py`, `dsh/test_scoring.py`, `docs/SCORING.md` (new) | **New capability, not portability.** Scores a student conversation point by point against a rubric that is traceable to graph nodes, and keeps an append-only transcript a teacher can verify. Independent of scope-guard; neither imports the other. See *Upstream Candidates*. | **Propose upstream** | Pure addition; nothing existing calls it |
| `dsh/guards/scope_guard.py`, `dsh/test_scope_guard.py`, `docs/SCOPE_GUARD.md`, `templates/scope/unit.example.json` (new), `dsh/agent_loop.py` (opt-in mount) | **New capability, not portability.** Confines one conversation to a declared region of the graph, enforced in the tool pipeline rather than by prompt text. See *Upstream Candidates*. | **Propose upstream** | `AgentLoop` mounts it only when a `scope` is passed; default behaviour is byte-identical |
| `requirements.txt` (new) | The project shipped no dependency manifest. | **Propose upstream** | — |
| `docs/WINDOWS.md`, `FORK_NOTICE.md` (new) | Setup, the platform behaviour table, attribution. | Retain | — |

**Upgrade method.** Mechanical transforms are replayed by
`tools/port_codemod.py` (self-tested: 8 cases plus an idempotency check).
The hand-written fixes are **not** replayable — they must survive as an
ordinary git merge. Taking the upstream tree wholesale and re-running the
codemod was measured at 45/82 passing versus 57/82 for a real merge, so
wholesale replacement is not a valid upgrade path for this downstream.

## Declared Protocols And Checks

```text
agent task protocol:  unchanged from upstream 0.10.0
semantic IR protocol: unchanged from upstream 0.10.0
graph plan protocol:  unchanged from upstream 0.10.0
conformance commands:
  - python .scripts/graph_validate.py
  - python -m compileall -q .scripts/
  - python tools/runtests.py <repo> <results.txt>   # runs every test_*.py
  - python tools/port_codemod.py --self-test
```

No public contract, data format, or method is changed by this downstream.
The frozen paper artifacts are byte-identical to upstream.

### Reproducible results

Windows 11 (26200), Python 3.14, same machine, 82 test files, 2026-09-29:

| Tree | Pass | Fail |
| --- | --- | --- |
| Clean upstream v0.10.0 (`0e87e3b`) | 41 | 41 |
| This downstream (`e393701`) | **57** | 25 |

16 upstream failures fixed, **0 regressions**. The same comparison at the
v0.8.0 baseline gave 40/81 versus 56/81 — also +16 with 0 regressions.

Re-measured 2026-10-09 after the separator, subprocess and symlink fixes
listed above, against a clean `git worktree` of the preceding commit so that
both trees run on the same machine on the same day:

| Tree | Pass | Fail |
| --- | --- | --- |
| This downstream at `b11b59f` | 57 | 25 |
| This downstream, current | **61** | 21 |

`test_wg.py`, `test_private_query.py`, `test_query_orchestrate.py` and
`dsh/test_dsh_harness.py` went FAIL → PASS; **0 regressions**. Counting
`dsh/test_scope_guard.py`, the current tree is 62/83.

Each of those four failures was the port's own gap, not an upstream defect:
the separator pass had matched a narrower pattern than the one in use, the
UTF-8 subprocess pass had missed `query_actions.graph_query`, and the symlink
skip had missed `test_private_query.py`. They surfaced only when the tree was
driven from an application rather than from its own tests — a reminder that
`runtests.py` passing is weaker evidence than it looks, because the runner
exports `PYTHONUTF8=1` and so hides exactly the class of defect in (2).

The 21 remaining failures are shared with clean upstream and are not caused
by the port. They have not yet been classified.

`python .scripts/graph_validate.py` reports 0 ERROR (171 WARN, all
`missing_semantic_description` on this downstream's own topic pages).

## Domain Evaluations

Kept separate from the contract checks above. These measure answer quality,
not compatibility.

| Set | Size | Source | Answers | Redistribution |
| --- | --- | --- | --- | --- |
| Course homework, chapter 1 | 20 items | Instructor-provided | Yes, official | **No** — instructor's material |
| Auto-generated questions | 130 items | Model-generated from the same courseware | Self-referential | Internal only |
| Textbook exercises | 74 problems | Liang Kunmiao, 5th ed. | Final answers only | **No** — copyrighted |
| Study-guide exercises | 56 problems / ~159 parts | Yang Kongqing et al. | **Full worked solutions** | **No** — copyrighted |

Configuration: retrieval bge-m3 (local), answering Qwen3-8B-Q4 via Ollama
(local), judging GLM-4.6V (cloud, deliberately a different model from the
one answering).

**Accepted thresholds and known limitations.**

- Best verified result: **16/20 (80%)** on the instructor's homework, by
  item-by-item human verification. The LLM judge reported 19/20.
- **The judge over-reports on any question whose conclusion appears in the
  question itself** — "prove that X = 1", "does the limit exist". Restating
  the conclusion matches the reference answer, and the judge has nothing to
  check. All three over-reported items were of this kind. Proofs and
  existence questions must be judged by a human; computation and concept
  questions agreed with human judgement exactly.
- **Self-generated questions overestimate by a wide margin**: 96% on the
  130 auto-generated items versus 80% on the same system with real homework.
- Open weakness: 0/2 on limit-existence problems. The model applies
  L'Hôpital's rule to non-analytic functions. Adding the textbook section
  that contains the correct method did not fix it, and widening the
  retrieval window from 8 to 20 made it worse.

## Data, Privacy, Security, And License

- **Domain data authority.** Courseware belongs to the course instructor,
  who has permitted API processing but not redistribution. Textbooks are
  copyrighted. None of it is committed: `teaching/wiki/topics/`,
  `cross-domain/graph.db` and `.env` are untracked in this repository, and
  no course or textbook content appears in any commit.
- **Remote processing.** Formula recognition and judging call a cloud model.
  Retrieval and answering run locally. Credentials live in `.env`, which is
  untracked.
- **Inherited boundaries.** The Raw, provenance, graph-write, and publication
  boundaries inherited from Ran-ASKS are not weakened. No deviation to
  declare.
- **License.** Follows the upstream license; see `FORK_NOTICE.md`.
- **Incident contact.** The issue tracker above.

## Upstream Candidates

All portability work listed under *Core Files Modified* as "Propose
upstream" is domain-independent: it concerns file encoding, path separators,
resource handles and platform-specific commands, and none of it mentions
this downstream's subject matter. Roughly 68% of it is a no-op on POSIX.

Submitted as https://github.com/ranshiju/Ran-ASKS/pull/1 against the v0.6.0
baseline (140 files). That proposal is now stale relative to v0.10.0 and its
size does not match `CONTRIBUTING.md`'s request for focused proposals with
regression tests. It should be withdrawn and resubmitted as separate
proposals, each with its own failing-then-passing test:

1. Pin text writes to LF, and mark `paper-artifacts/**` as `-text`.
   Motivated by a data-contract violation, not by Windows.
2. Emit repository-relative paths as POSIX consistently.
3. Close SQLite handles explicitly.
4. Decode subprocess output as UTF-8 explicitly.
5. Import PyMuPDF under the `pymupdf` name with a `fitz` fallback.
6. `platform_compat.py` plus its graph registration.
7. `read_section.py` plus its whitelist entry.
8. `requirements.txt`.
9. **scope-guard** — conversation scope control (`dsh/guards/scope_guard.py`).
   Not portability: a capability this downstream needs and proposes back.
   A scope is declared as a contract over the graph — named root nodes, a
   transitive closure over chosen predicates (`前置`), and a one-hop
   attachment of sources and keywords — and is enforced at
   `tools/pre-execute` (deny a named read outside the region) and
   `tools/post-execute` (filter retrieval envelopes), not by prompt text.
   Unresolvable roots raise rather than silently shrinking the region.
   34 tests, all on synthetic graphs, no domain data. Opt-in: `AgentLoop`
   without a `scope` behaves exactly as before.
10. **conversation-scoring** — rubric scoring with a verifiable transcript
   (`dsh/scoring.py`). Also a capability, not portability. Three decisions
   carry it: judge each rubric point separately rather than the answer as a
   whole (whole-answer judging over-reported by 25% in this downstream's
   measurements); make the verdict three-valued, so that a point the
   knowledge base cannot support is `ungradable` and **leaves the
   denominator** rather than costing the student marks — ASKS withholds
   answers it cannot ground, and that must not read as a wrong answer; and
   require every rubric point to name the graph node it came from, validated
   the way `prerequisites` is. The judge is injected, so ASKS does not decide
   which model — or whether a model at all — does the grading. 21 tests, no
   domain data, no model calls.

Four Core defects reported earlier were fixed upstream in v0.7.0–v0.8.0:
inline comments not stripped from `.env`, the hardcoded embedding API path,
an invalid default `EMBED_MODEL`, and `--triples-json` not substituting the
"this file" subject pronoun.
