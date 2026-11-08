#!/usr/bin/env bash
# Refuse `git commit` unless the committer is Rossano.
#
# Every commit on this repository is authored and committed by
# Rossano <rossano.schifanella@gmail.com> — no co-author trailer, no agent
# attribution. The `attribution` block in .claude/settings.json removes the
# trailers an agent would otherwise append; this hook covers the half that
# cannot be fixed afterwards. A wrong `user.email` is invisible until the
# commit already carries it, and by then only rewriting history removes it.
#
# Deliberately free of `jq`: a guard that depends on a tool the machine may
# not have is a guard that fails on the one machine it mattered on. Reads the
# PreToolUse payload on stdin; exits 0 (allow) for anything that is not a
# commit, and for a commit under the expected identity.

set -uo pipefail

EXPECTED="rossano.schifanella@gmail.com"

payload="$(cat)"

# `git ... commit` as whole words. `commit` must be its own token, so
# `git log --grep=commit` is left alone; and `git` must not be the tail of a
# longer word, so `mygit` is not git — but a leading quote or slash is a
# boundary, because the command arrives inside the JSON payload and may be
# spelled `/usr/bin/git`.
if ! printf '%s' "$payload" |
    grep -qE '(^|[^[:alnum:]_-])git([[:space:]]+[^[:space:]]+)*[[:space:]]+commit([^[:alnum:]_]|$)'; then
    exit 0
fi

actual="$(git config user.email 2>/dev/null || true)"
[ "$actual" = "$EXPECTED" ] && exit 0

# Keep the reason free of characters that would need JSON escaping.
safe="$(printf '%s' "${actual:-<unset>}" | tr -d '"\\')"

printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"refusing to commit as %s - this repository is authored and committed by Rossano <%s> only. Fix with: git config user.name Rossano && git config user.email %s"}}' \
    "$safe" "$EXPECTED" "$EXPECTED"
