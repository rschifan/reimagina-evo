"""Every command a runbook script invokes must exist, with the flags it passes.

`scripts/acceptance_run.sh` and `scripts/mexicali_run.sh` are long sequences of
CLI calls. When a flag is renamed, nothing here fails: the script is shell, the
CLI is Python, and the two meet only when someone runs the script. On the
acceptance run that means a failure twenty minutes in; on the Mexicali run it
can mean a failure *after* a ~3 GB INEGI filter and a 387k-row Overture load.

So this reads the scripts, pulls out every `$CLI …` invocation and every
`--flag` on it, and checks both against the live Typer command tree. It is a
guardrail of the kind `tests/contract` exists for: it tests the repository, not
the code.

⚠ It cannot check argument *values* — that `--cve-ent 02` names a state whose
data is in the mirror is not knowable from here. It checks that the command and
the flag exist, which is the failure mode that costs a re-run.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer.main

from apps.cli.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("scripts/acceptance_run.sh", "scripts/mexicali_run.sh")

#: Flags the scripts pass that are not declared on the subcommand, because the
#: shell supplies them from the environment or they are global. Empty on
#: purpose — an entry here is a hole, so it should have to be argued for.
_ALLOWED_UNDECLARED: dict[str, set[str]] = {}


def _command_tree() -> dict[tuple[str, ...], set[str]]:
    """Map every command path to the set of option strings it accepts."""
    root = typer.main.get_command(app)
    tree: dict[tuple[str, ...], set[str]] = {}

    def walk(cmd, path: tuple[str, ...]) -> None:
        opts: set[str] = set()
        for param in getattr(cmd, "params", []):
            opts.update(getattr(param, "opts", []) or [])
            opts.update(getattr(param, "secondary_opts", []) or [])
        tree[path] = opts
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                walk(sub, path + (name,))

    walk(root, ())
    return tree


#: `$CLI word word --flag …` up to the end of the logical line, following
#: backslash continuations, which every multi-line invocation in these scripts
#: uses.
_INVOCATION = re.compile(r"\$CLI\s+((?:[^\n\\]|\\\n)+)")

#: Shell tokens that end the command and begin something else. Without this,
#: `$CLI indicators compute … | tr -d '\n'` contributes `-d` as if the CLI had
#: been passed it.
_OPERATORS = {"|", "||", "&&", ";", "&", ">", ">>", "<"}


def _close_substitution(fragment: str) -> str:
    """Cut the fragment at the `)` that closes an enclosing `$( … )`.

    `acceptance_run.sh` wraps invocations in command substitution, so the
    sliced fragment ends `--detach)"`. That trailing `)"` opens an unterminated
    quote, shlex gives up, and the fallback split reports `--detach)"` as an
    undeclared flag. Cutting at the first unmatched `)` removes it without
    needing to parse the shell.
    """
    depth = 0
    for index, char in enumerate(fragment):
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return fragment[:index]
            depth -= 1
    return fragment


def _tokenize(fragment: str) -> list[str]:
    """Shell-aware split of one `$CLI` invocation, truncated at the first
    operator.

    ⚠ `shlex` rather than `str.split`, and the reason is a real false positive:
    `--source-name "Overture Buildings - Mexicali BBox"` splits on whitespace
    into a bare `-`, which then looks like an undeclared flag. shlex keeps the
    quoted value as one token, so a value can contain anything.
    """
    joined = _close_substitution(fragment.replace("\\\n", " "))
    try:
        tokens = shlex.split(joined, posix=True)
    except ValueError:
        # Unbalanced quote in the fragment we sliced; fall back rather than
        # erroring, and let the command/flag assertions judge what is there.
        tokens = joined.split()
    cut = len(tokens)
    for index, token in enumerate(tokens):
        if token in _OPERATORS or re.fullmatch(r"\d*>>?.*", token):
            cut = index
            break
    return tokens[:cut]


def _invocations(text: str) -> list[list[str]]:
    return [_tokenize(m.group(1)) for m in _INVOCATION.finditer(text)]


def _split(tokens: list[str], tree) -> tuple[tuple[str, ...], list[str]]:
    """Split an invocation into its command path and the flags it passes.

    The command path is the longest leading run of tokens that names a real
    command, so `indicators compute` resolves to two words while
    `bootstrap-db` resolves to one, without a table of arities.
    """
    path: tuple[str, ...] = ()
    index = 0
    while index < len(tokens) and not tokens[index].startswith("-"):
        candidate = path + (tokens[index],)
        if candidate not in tree:
            break
        path = candidate
        index += 1
    flags = [t.split("=", 1)[0] for t in tokens[index:] if t.startswith("-")]
    return path, flags


@pytest.fixture(scope="module")
def tree():
    return _command_tree()


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_exists_and_invokes_the_cli(script):
    path = REPO_ROOT / script
    assert path.is_file(), f"{script} is referenced by this test and missing"
    assert _invocations(path.read_text()), (
        f"{script} contains no $CLI invocations — either it stopped using the "
        f"CLI or the $CLI convention changed, and this test has gone blind"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_invoked_command_exists(script, tree):
    text = (REPO_ROOT / script).read_text()
    unknown = []
    for tokens in _invocations(text):
        command_path, _ = _split(tokens, tree)
        # A bare `$CLI --flag` or a command whose first word is a shell
        # variable cannot be resolved; the leading word must name a command.
        if not command_path:
            first = tokens[0] if tokens else "(empty)"
            if first.startswith("$"):
                continue
            unknown.append(" ".join(tokens[:3]))
    assert not unknown, (
        f"{script} invokes commands that do not exist: {unknown}. "
        f"Either the command was renamed or the script was written against a "
        f"CLI that is no longer there."
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_every_flag_passed_is_declared(script, tree):
    text = (REPO_ROOT / script).read_text()
    problems = []
    for tokens in _invocations(text):
        command_path, flags = _split(tokens, tree)
        if not command_path:
            continue
        declared = tree[command_path]
        allowed = _ALLOWED_UNDECLARED.get(" ".join(command_path), set())
        for flag in flags:
            if flag not in declared and flag not in allowed:
                problems.append(f"{' '.join(command_path)} {flag}")
    assert not problems, (
        f"{script} passes flags the CLI does not declare: {sorted(set(problems))}. "
        f"A renamed flag fails only when someone runs the script — on the "
        f"Mexicali run, after a 3 GB INEGI filter and a 387k-row load."
    )


def test_the_mexicali_runbook_computes_membership_after_every_load(tree):
    """⚠ Order is load-bearing and its failure mode is silent.

    `get_tile` scopes by AOI through an EXISTS against `aoi_feature`. A feature
    loaded *after* the membership pass is correctly typed, visible in
    `serving_geo`, and returns 204 on every tile with no error anywhere in the
    path. That is how the street layer broke once: 40 road features, all
    present, all undrawable.

    So `aoi compute-features` must come after every ingest step in the script.
    """
    text = (REPO_ROOT / "scripts/mexicali_run.sh").read_text()
    positions = {}
    for match in _INVOCATION.finditer(text):
        tokens = match.group(1).replace("\\\n", " ").split()
        command_path, _ = _split(tokens, tree)
        key = " ".join(command_path)
        if key and key not in positions:
            positions[key] = match.start()

    membership = positions.get("aoi compute-features")
    assert membership is not None, (
        "the Mexicali runbook must compute AOI membership; without it every "
        "tile returns 204"
    )
    for loader in (
        "inegi-marco bootstrap",
        "inegi-censo bootstrap",
        "points bootstrap",
        "overture load",
        "network bootstrap",
    ):
        where = positions.get(loader)
        assert where is not None, f"{loader} missing from the Mexicali runbook"
        assert where < membership, (
            f"{loader} runs after `aoi compute-features`, so the features it "
            f"loads will be in no AOI — present everywhere except the map, "
            f"with no error to find"
        )


def test_the_network_command_the_accessibility_guard_names_exists(tree):
    """The guard's `loader=` hint has to be runnable.

    It used to read "`reimagina.pipeline.osm_network.bootstrap_network`" — a
    Python function, offered to someone whose indicator had just refused to
    run. Every `loader=` hint is now a command, and this is the one that had
    no command behind it at all until it was added.
    """
    from reimagina.indicators import accessibility, building_density

    sources = [
        Path(accessibility.__file__).read_text(),
        Path(building_density.__file__).read_text(),
    ]
    hints = []
    for source in sources:
        hints += re.findall(r'loader="`([^`]+)`"', source)
    assert hints, "no loader hints found; this test has gone blind"

    for hint in hints:
        assert hint.startswith("python -m apps.cli.app "), (
            f"loader hint {hint!r} is not a runnable command — a user who hits "
            f"this guard is being told to write Python"
        )
        tokens = hint.removeprefix("python -m apps.cli.app ").split()
        command_path, _ = _split(tokens, tree)
        assert command_path, f"loader hint {hint!r} names no real command"

    assert ("network", "bootstrap") in tree, (
        "`network bootstrap` is the only way to populate curated_geo.road_edge "
        "from real OSM data; without it the accessibility family and the "
        "street-context layer are unreachable from the CLI"
    )


def test_the_scripts_mexicali_bbox_matches_the_python_constant():
    """⚠ The same box written twice, in two languages.

    `scripts/mexicali_run.sh` passes `--clip-bbox` to bound the OSM fetch, and
    `overture extract` defaults to `MEXICALI_BBOX` for the buildings. If the
    two drift, the street layer and the building layer cover different ground
    — and nothing fails: you get a city whose streets stop where its buildings
    do not. Nothing else compares them.
    """
    from reimagina.pipeline.overture import MEXICALI_BBOX

    text = (REPO_ROOT / "scripts/mexicali_run.sh").read_text()
    match = re.search(r'^MEXICALI_BBOX="([^"]+)"', text, re.MULTILINE)
    assert match, "scripts/mexicali_run.sh no longer defines MEXICALI_BBOX"

    from_script = tuple(float(v) for v in match.group(1).split(","))
    assert from_script == MEXICALI_BBOX, (
        f"the runbook's clip box {from_script} differs from "
        f"reimagina.pipeline.overture.MEXICALI_BBOX {MEXICALI_BBOX}; the OSM "
        f"fetch and the Overture extract would cover different ground"
    )
