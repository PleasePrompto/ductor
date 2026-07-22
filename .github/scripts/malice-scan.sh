#!/usr/bin/env bash
# Scans the ADDED lines of a git diff for high-signal malicious-code patterns.
#
# Two tiers:
#   BLOCK  (exit 1): code-execution / obfuscation / reverse-shell / trojan-source
#          primitives that are never used in this codebase — each is worth a
#          human look before merge.
#   REPORT (exit 0): new dependencies, network endpoints, credential names and
#          encoded blobs — surfaced to the job summary so a reviewer notices
#          them, but not a merge blocker. Following GuardDog's model, weak
#          signals (a URL, a requests call) are reported, not blocked; only the
#          rarely-legitimate execution primitives block.
#
# Usage: malice-scan.sh <base-ref>       e.g. malice-scan.sh origin/main
# ponytail: high-signal blocklist, not a sandbox. A determined attacker can
# obfuscate around it — the goal is to force human review on the obvious stuff,
# not to be a complete malware detector. Deep coverage is pip-audit's job.
# Pattern sources: DataDog GuardDog, OSSF package-analysis, Trojan Source
# (CVE-2021-42574).
set -euo pipefail

BASE="${1:?usage: malice-scan.sh <base-ref>}"
summary="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

# Exclude this script from its own scan: its regex literals (/dev/tcp/,
# AWS_SECRET_ACCESS_KEY, curl|sh, …) would otherwise self-match. Changes to the
# scanner itself are always security-reviewed in the PR.
self=':(exclude).github/scripts/malice-scan.sh'
added_py="$(git diff --no-color "${BASE}...HEAD" -- '*.py' "$self" | grep -E '^\+[^+]' || true)"
added_all="$(git diff --no-color "${BASE}...HEAD" -- . "$self" | grep -E '^\+[^+]' || true)"

# --- BLOCK tier -------------------------------------------------------------
# Never-legitimate-here primitives in added Python. \bexec\( does NOT match
# create_subprocess_exec ('_' before 'exec' is a word char, so \b fails) nor
# run_in_executor ('executor' is not followed by '('); \beval\( skips evaluate.
blockers='\beval[[:space:]]*\('                       # arbitrary code eval
blockers+='|\bexec[[:space:]]*\('                      # arbitrary code exec
blockers+='|\bos\.popen[[:space:]]*\('                 # shell command exec
blockers+='|\bos\.system[[:space:]]*\('                # shell command exec
blockers+='|\bpty\.spawn[[:space:]]*\('                # reverse-shell primitive
blockers+='|\b__import__[[:space:]]*\('                # dynamic import (import-scan evasion)
blockers+='|getattr[[:space:]]*\([[:space:]]*__builtins__'  # eval/exec via builtins
blockers+='|__builtins__[[:space:]]*\['                # eval/exec via builtins
blockers+='|\bpickle\.loads\b'                         # deserialization RCE
blockers+='|\bmarshal\.loads\b'                        # bytecode payload
blockers+='|\bcodecs\.decode[[:space:]]*\('            # rot13/hex obfuscation
blockers+='|\bbase64\.b(16|32|64)decode\b'             # encoded payload
blockers+='|\bshell[[:space:]]*=[[:space:]]*True\b'    # shell injection surface
blockers+='|\bsocket\.socket[[:space:]]*\('            # raw socket (exfil/reverse shell)

py_hits="$(printf '%s\n' "$added_py" | grep -nP "$blockers" || true)"

# Cross-file blockers (not just .py): reverse shells and trojan-source hide in
# shell, yaml, config too.
#   /dev/tcp/  -> bash reverse shell
#   bidirectional/override unicode -> Trojan Source (CVE-2021-42574)
shell_hits="$(printf '%s\n' "$added_all" | grep -nP '/dev/tcp/' || true)"
bidi_hits="$(printf '%s\n' "$added_all" | grep -nP '[\x{202A}-\x{202E}\x{2066}-\x{2069}]' || true)"

hits="$(printf '%s\n%s\n%s\n' "$py_hits" "$shell_hits" "$bidi_hits" | grep -vE '^$' || true)"

if [[ -n "$hits" ]]; then
  {
    echo "### ❌ Malice scan: high-signal pattern(s) in added code"
    echo ""
    echo "Review each before merging. If a hit is a legitimate false positive,"
    echo "a maintainer can merge past this required check."
    echo '```'
    printf '%s\n' "$hits"
    echo '```'
  } >>"$summary"
  echo "::error::Malice scan found high-signal execution/obfuscation/reverse-shell patterns in added code."
  printf '%s\n' "$hits"
  exit 1
fi

# --- REPORT tier (non-blocking) --------------------------------------------
deps="$(git diff --no-color "${BASE}...HEAD" -- pyproject.toml uv.lock \
  | grep -E '^\+' | grep -vE '^\+\+\+' \
  | grep -iE 'name = "|^\+ +"[a-z0-9_.-]+(>=|==|~=|<|\[)' || true)"

net="$(printf '%s\n' "$added_py" \
  | grep -nP '\b(import[[:space:]]+(socket|urllib|http\.client|smtplib|ftplib|telnetlib)|from[[:space:]]+(socket|urllib|http|smtplib)|requests\.|httpx\.|urllib\.request|https?://[^"'"'"' ]+)' \
  | grep -vP 'x\.ai|anthropic\.com|googleapis\.com|example\.(test|invalid|com)|schemas\.|w3\.org' || true)"

# GuardDog "shady-links": suspicious TLDs and URL shorteners.
shady="$(printf '%s\n' "$added_all" \
  | grep -niP 'https?://[^"'"'"' ]*\.(xyz|top|tk|gq|ml|cf|ru|su)\b|https?://(bit\.ly|tinyurl\.com|is\.gd|t\.co|goo\.gl)/' || true)"

# Exfiltration indicators: cloud-credential names and credential file paths.
creds="$(printf '%s\n' "$added_all" \
  | grep -nP 'AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|\.aws/credentials|\.ssh/id_(rsa|ed25519|dsa)|getpass\.getuser[[:space:]]*\(|socket\.gethostname[[:space:]]*\(' || true)"

# Long base64-ish blobs in added Python: classic encoded-payload shape.
blobs="$(printf '%s\n' "$added_py" | grep -nP '[A-Za-z0-9+/]{200,}={0,2}' || true)"

# curl/wget piped straight into a shell.
pipesh="$(printf '%s\n' "$added_all" | grep -nP '(curl|wget)\b[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh\b' || true)"

emit() {  # emit <title> <body>
  [[ -z "$2" ]] && return 0
  { echo ""; echo "<details><summary>$1</summary>"; echo ""; echo '```'
    printf '%s\n' "$2"; echo '```'; echo "</details>"; } >>"$summary"
}

echo "### ✅ Malice scan: no blocking patterns" >>"$summary"
emit "ℹ️ Dependency changes — confirm each is intended" "$deps"
emit "ℹ️ Network imports / URLs — confirm each is expected" "$net"
emit "⚠️ Suspicious TLDs / URL shorteners" "$shady"
emit "⚠️ Credential names / paths — confirm no exfiltration" "$creds"
emit "⚠️ Long base64-like blobs — confirm not an encoded payload" "$blobs"
emit "⚠️ Pipe-to-shell (curl|wget → sh)" "$pipesh"

echo "Malice scan: no high-signal patterns in added Python."
