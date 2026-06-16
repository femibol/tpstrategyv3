"""Safe VPS deploy script (`scripts/deploy-vps.sh`).

2026-06-16 incident: the previous one-shot deploy command used
`git reset --hard origin/main`. PR #215 had `git rm --cached
data/trade_history.json` (the file moved to .gitignore), but the
host still had a modified copy in its working tree. The reset
followed origin's view and deleted the file — 297 trades of
analytics history wiped on a Saturday afternoon, recovered only
because the claude/live-state branch had been snapshotting it.

This script encodes the lessons:

  1. Back up `data/*.json` BEFORE any git operation.
  2. Use `git checkout origin/main -- <code paths>` (targeted),
     never `git reset --hard origin/main` (whole-tree, destructive).
  3. Stash `config/` edits up front.
  4. Verify dashboard /health before exiting OK.
  5. Prune old backups so disk doesn't fill.

These tests pin those invariants in the script source. Live behavior
on the VPS is tested out-of-band when the deploy actually runs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "deploy-vps.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "deploy script missing"
    mode = SCRIPT.stat().st_mode
    assert mode & 0o111, "deploy script must be executable (chmod +x)"


def test_script_starts_with_bash_shebang():
    line = SCRIPT.read_text().splitlines()[0]
    assert line == "#!/bin/bash", f"unexpected shebang: {line!r}"


def test_uses_targeted_checkout_not_reset_hard():
    """The 2026-06-16 incident was `git reset --hard` deleting
    `data/trade_history.json`. The hardened script must NOT use that
    pattern anywhere — instead it uses `git checkout origin/<branch>
    -- <paths>` so only code paths are touched."""
    src = SCRIPT.read_text()
    # Forbid the dangerous reset pattern on any executable line
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r"git\s+reset\s+--hard", stripped), (
            f"deploy script uses dangerous `git reset --hard` on line: {line!r}"
        )
    # Require the actual targeted command (not the explanation in the
    # header comment — anchor on the literal command form).
    assert re.search(r'git\s+checkout\s+"origin/\$BRANCH"', src), (
        "expected `git checkout \"origin/$BRANCH\" -- ...` as the deploy "
        "mechanism"
    )


def test_backs_up_data_before_touching_git():
    """Belt-and-suspenders: even if git ever did try to remove a
    data file, we'd recover from the backup. The cp loop must
    appear BEFORE the git fetch/checkout commands."""
    src = SCRIPT.read_text()
    backup_idx = src.find('cp -a "$f" "data-backup/')
    # Anchor on the COMMAND (not the comment) for fetch + checkout
    fetch_idx = src.find('git fetch origin')
    checkout_idx = src.find('git checkout "origin/$BRANCH"')
    assert backup_idx > 0, "data backup step missing"
    assert fetch_idx > 0 and checkout_idx > 0
    assert backup_idx < fetch_idx, "backup must run BEFORE git fetch"
    assert backup_idx < checkout_idx, "backup must run BEFORE git checkout"


def test_data_dir_is_never_in_code_paths():
    """The CODE_PATHS array enumerates what git touches. `data` must
    NEVER be in this list — that would re-introduce the bug."""
    src = SCRIPT.read_text()
    # Find the CODE_PATHS array
    m = re.search(r"CODE_PATHS=\((.*?)\)", src, re.DOTALL)
    assert m, "CODE_PATHS array not found"
    paths_block = m.group(1)
    # Allow `data-backup` only as a reference outside the array
    paths = re.findall(r"^\s*([A-Za-z_.-]+)\s*$", paths_block, re.MULTILINE)
    assert "data" not in paths, (
        "`data` is in CODE_PATHS — that re-introduces the 2026-06-16 bug "
        "where git rewrites of data/ wiped trade history"
    )
    # Bot code must be touched
    assert "bot" in paths
    assert "tests" in paths


def test_config_stashed_before_checkout():
    """Pre-PR-#212 code wrote auto-tuner edits to config/ files directly.
    Stashing them avoids a merge conflict when the checkout brings in
    a new version of the same files."""
    src = SCRIPT.read_text()
    stash_idx = src.find("git stash push")
    checkout_idx = src.find('git checkout "origin/$BRANCH"')
    assert stash_idx > 0, "stash step missing"
    assert stash_idx < checkout_idx, "stash must run BEFORE checkout"
    # Stash must scope to config/ specifically (not whole tree)
    stash_block = src[stash_idx:stash_idx + 300]
    assert "-- config/" in stash_block, "stash must be scoped to config/"


def test_restarts_container_via_docker_restart():
    """A restart is enough since `bot/` is bind-mounted (HANDOFF session
    9). Don't rebuild — that's slow and unnecessary."""
    src = SCRIPT.read_text()
    assert 'docker restart "$CONTAINER"' in src
    # Must NOT do a full rebuild — those are minutes-long and unnecessary
    assert "docker compose up --build" not in src
    assert "docker-compose up --build" not in src


def test_waits_for_health_before_exit():
    """A deploy that says OK while the container has crashed is worse
    than one that says FAILED. Verify /health is ok before exit 0."""
    src = SCRIPT.read_text()
    assert "curl" in src and "/health" in src
    # The loop must check for "status":"ok"
    assert '"status":"ok"' in src
    # Must exit non-zero on failure
    assert 'echo "deploy: ERROR' in src
    assert "exit 1" in src


def test_prunes_old_backups():
    """Disk fills over time if every deploy leaves a new backup. Keep
    the last 10 per filename."""
    src = SCRIPT.read_text()
    # Some form of `tail -n +11` cleanup must exist
    assert "tail -n +11" in src, "backup pruning missing"


def test_set_eu_pipefail_for_safety():
    """`set -euo pipefail` catches typos and silent failures — a
    deploy script with a typo'd path mustn't keep running and produce
    a fake 'deploy: ok' message."""
    src = SCRIPT.read_text()
    assert "set -euo pipefail" in src


def test_env_var_defaults_documented():
    """The script should document REPO_DIR / CONTAINER / BRANCH
    overrides so an operator can reuse it in staging/dev."""
    src = SCRIPT.read_text()
    for var in ["REPO_DIR", "CONTAINER", "BRANCH"]:
        assert f"{var}=" in src, f"env var {var} default missing"
