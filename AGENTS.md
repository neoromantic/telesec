# AGENTS.md

<!-- devkit:agents v1 -->
<!-- managed by devkit v1: edit in https://github.com/neoromantic/devkit/blob/main/agents/AGENTS.shared.md -->

## Shared rules (devkit)

These rules apply to people and coding agents in every repo that uses
[devkit](https://github.com/neoromantic/devkit). Project rules follow in `## Project` below.
Where they conflict, the project section wins.

### Data

- Use only made-up data in tests, fixtures, examples, docs and screenshots. No real people, chats,
  phone numbers, tokens, session files or local paths such as home directories.
- Never commit `.env` files with values, or credentials; `.env.example` and password-manager
  reference files are fine. Tests run offline by default; live API tests are opt-in and read
  their credentials from the environment.

### Commands

Use the project section's native commands for checks, offline tests and builds:

- Bun: `bun run <script>` (`check`, or `lint` and `typecheck`; `test`, `test:e2e`, `build`).
  `bun run format` formats; `check` fails on unformatted code and on lint errors.
- Python: the documented `uv run` / `uvx` commands, such as pytest, unittest and ruff.
- Go: the documented `go` commands from `go.mod`. Do not add Bun wrappers for Python or Go.
- Use Bun, uv or Go modules for installs. Use npm only for `npm publish` and `npm view`.

### Code

- Make the smallest change that works, then make it smaller. No drive-by refactors.
- Validate input at the boundaries (CLI arguments, HTTP, files, environment) and trust it
  inside: no fallbacks or checks for states that can't happen, no compatibility code for
  callers that don't exist. Published interfaces change only as the release procedure allows.
- No helpers or abstractions for logic with a single caller. More in the
  [code patterns standard](https://github.com/neoromantic/devkit/blob/main/docs/standards/code-patterns.md).

### Definition of done

- The change works end to end and the project's applicable checks and tests pass locally.
  Verify with targeted checks and builds for what changed; wait for a full CI run only when the
  task needs its result. Required checks still gate the merge.
- README, docs and affected demos change with the behaviour they describe; follow the
  [product content standard](https://github.com/neoromantic/devkit/blob/main/docs/standards/product-content.md).
- No new warnings, no commented-out code, no stray debug output.
- JS and TS follow the [lint and format standard](https://github.com/neoromantic/devkit/blob/main/docs/standards/lint-format.md):
  no prose comments (rationale goes in the PR), tests beside their code, no mocks of the code
  under test, no fixed sleeps or retries. Fix the code; exceptions go in `.oxlintrc.json`.
- A test names the behaviour it protects and fails on a believable bug. Strengthen the test that
  owns a contract instead of adding another; delete tests that repeat one or restate the code.
  Tests run in parallel workers, each with its own temp dir, port, database and environment
  ([testing standard](https://github.com/neoromantic/devkit/blob/main/docs/standards/testing.md)).
- A pull request's CI runs one runtime version and only the jobs its diff can affect; other
  versions and platforms run after merge, on a schedule or on request
  ([CI speed standard](https://github.com/neoromantic/devkit/blob/main/docs/standards/ci-speed.md)).

### Commits and releases

- Conventional Commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`, `ci:`.
  A breaking change adds `!` (`feat!:`) and a `BREAKING CHANGE:` footer. In a monorepo the
  scope names the workspace (`feat(web):`). With squash merges the pull request title is the
  commit title and follows the same format.
- One logical change per commit. Never rewrite published history or force-push `main`.
- Follow the project's documented release procedure, including any owner-only steps. It defines
  versioning, tags, changelog and delivery (release, skill revision or production deployment).

### Files

- Don't add planning or progress files (`progress.md`, `docs/plans/`). Plans live in issues.
  `ROADMAP.md`, if present, is a short "Known gaps" list that links issues.
- Don't add agent hooks that rewrite tracked files.
- `AGENTS.md` is the canonical instruction file. `CLAUDE.md` contains only `@AGENTS.md`.
- Files marked "managed by devkit" (this block, `SECURITY.md`, `CONTRIBUTING.md`, the PR
  template, `.gitleaks.toml`, `.github/zizmor.yml`, `renovate.json`, `lefthook.yml`, the
  CI stub, `.oxfmtrc.jsonc`, the `.oxlintrc.devkit*.jsonc` configs and the usage telemetry
  clients) are synced from devkit. Change them there, not here.

<!-- /devkit:agents -->

## Project

Telesec — Telegram Business secretary for Hermes: voice transcription, correspondence history, stats, and dossiers. Runtime plugin ID `telesec`. Product direction lives in [docs/ROADMAP.md](docs/ROADMAP.md). Contributor setup and rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

### Every user-visible change

- **Docs in the same PR.** Update `README.md`, `CHANGELOG.md`, and the relevant `docs/*.md` (ROADMAP, VERIFY, feature guides) together with the code.
- **Website.** After a release the maintainer updates https://telesec.sergeybaus.com (version, supported Hermes version, features, screenshots). Its source is not in this repo, so mention website-relevant changes in the PR description.

### Release and deploy

- `main` is protected; use `gh pr merge --squash --auto --delete-branch` to merge after required checks pass.
- Releases are cut by hand until a release workflow exists; this overrides the shared release rule above. Release: bump `plugin.yaml`, `pyproject.toml`, `uv.lock`, and the version asserts in `tests/test_plugin.py`; date the CHANGELOG section, tag `vX.Y.Z`, publish a GitHub release.
- Releases are checked on the maintainer's running Hermes gateway: deploy the tag there, restart, check that the plugin's handlers are wired and the error log has no plugin lines, then run the smoke below. A clean isolated `hermes plugins validate --install-deps` is not part of every release; it is slow, so run it only when asked. Because a long-running gateway can have packages a clean Hermes lacks (0.10.0 failed to import on Hermes 0.22, which no longer installs PyYAML), plugin code may import only the standard library, PTB, and Hermes core dependencies. Hermes resolves the plugin's `[dependency-groups]` together with its own pins and disables the plugin on the next dependency refresh (0.12.0 hit this), so dependency groups name packages only and `uv.lock` pins versions.
- After a deploy, run the [controlled live smoke](docs/VERIFY.md#controlled-live-smoke) and record the result in `docs/VERIFY.md`. Keep that record content-free and host-neutral: no contact names, message text, raw IDs, host names, service unit names, or local paths.

### Hermes plugin catalog

- The entry is `plugin-catalog/telesec.yaml` in `NousResearch/hermes-agent` ([submission notes](docs/CATALOG_SUBMISSION.md)). Catalog users get a release only through a new upstream PR that changes the full 40-character `sha` and `version` together. Image URLs in the entry are raw GitHub URLs pinned to the same SHA.
- The catalog page renders `README.md` at the pinned SHA and drops raw HTML, so use Markdown images and links, not `<img>`.
- Open the PR from the `neoromantic` fork. Commit with the GitHub noreply email, because the contributor check rejects unmapped emails. The PR may only touch `plugin-catalog/**`. Workflows on a fork PR may wait for a maintainer to approve them.
- After each catalog change, check that the website install steps and version still match.
- Never name or compare Telesec with Nous's own Telegram Business plugin. Say it is not affiliated with Nous Research.

### Code

- Python with uv: `uv sync`, then `uv run --with pytest-xdist pytest -q -n auto` and `uv run ruff check .`. CI runs both on the Python in `.python-version` (3.14, the version Hermes installs); `requires-python` stays `>=3.11`, and ruff's `py311` target guards that floor. pytest-xdist comes in with `--with`, not the `dev` group, because Hermes resolves that group with its own pins. Tests must pass in any order and on any worker. `.github/workflows/ci.yml` is a hand-edited devkit stub with the parallel test command, so devkit sync leaves it alone. Keep the handler group and claim behaviour described in the README.
- Usage stats: `telemetry.py` is the devkit-managed client ([standard](https://github.com/neoromantic/devkit/blob/main/docs/standards/telemetry.md)); never edit it here. Hermes catalog rule 13 needs opt-in, while the client is on by default, so `usage_stats._status()` is the gate: on only while the owner's stored choice is `enabled: true` with the notice seen (Settings or `telemetry on`), whatever the client decided from `TELESEC_TELEMETRY=1`; an off from the client stays a veto. Every event goes through it, and nothing calls the client's `show_notice()` or `mark_notice_seen()`. The wiring is `usage_stats.py`: feature names come from its fixed lists, and errors pass a type or code only, never text that can quote chat content. Tests keep it off unless they patch the client's CI and test checks.
- GitHub issues, PRs, and docs are in simple English.
