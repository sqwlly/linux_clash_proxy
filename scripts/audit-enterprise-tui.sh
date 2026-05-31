#!/usr/bin/env bash

set -u -o pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${1:-$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)}"

failures=0
warnings=0

say() {
    printf '%s\n' "$*"
}

pass() {
    say "[PASS] $*"
}

warn() {
    warnings=$((warnings + 1))
    say "[WARN] $*"
}

fail() {
    failures=$((failures + 1))
    say "[FAIL] $*"
}

has_rg() {
    command -v rg >/dev/null 2>&1
}

pattern_exists() {
    local path="$1"
    local pattern="$2"

    if [ ! -e "$path" ]; then
        return 2
    fi

    if has_rg; then
        rg -q -- "$pattern" "$path"
    elif [ -d "$path" ]; then
        grep -REq -- "$pattern" "$path"
    else
        grep -Eq -- "$pattern" "$path"
    fi
}

check_file() {
    local rel_path="$1"
    local description="$2"
    if [ -f "${ROOT_DIR}/${rel_path}" ]; then
        pass "${description}: ${rel_path}"
    else
        fail "${description}: missing ${rel_path}"
    fi
}

check_contains() {
    local rel_path="$1"
    local pattern="$2"
    local description="$3"
    if pattern_exists "${ROOT_DIR}/${rel_path}" "$pattern"; then
        pass "$description"
    else
        fail "$description"
    fi
}

check_warn_missing() {
    local rel_path="$1"
    local pattern="$2"
    local description="$3"
    if pattern_exists "${ROOT_DIR}/${rel_path}" "$pattern"; then
        pass "$description"
    else
        warn "$description"
    fi
}

check_no_go_rewrite() {
    if [ -f "${ROOT_DIR}/go.mod" ]; then
        fail "Go rewrite guard: go.mod exists"
    else
        pass "Go rewrite guard: no go.mod"
    fi

    if has_rg; then
        if rg -q \
            -g '!deep-research-report.md' \
            -g '!docs/enterprise-tui/**' \
            -g '!*.pyc' \
            -g '!.data/**' \
            -- 'charm\.land/bubbletea|github\.com/charmbracelet/bubbletea' "${ROOT_DIR}"; then
            fail "Go rewrite guard: Bubble Tea dependency found outside source report/docs"
        else
            pass "Go rewrite guard: no Bubble Tea runtime dependency"
        fi
    else
        warn "Go rewrite guard: ripgrep missing, skipped Bubble Tea dependency scan"
    fi
}

report_git_scope() {
    if ! git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        warn "Git change report: not a git worktree"
        return
    fi

    local changed
    changed="$(
        {
            git -C "$ROOT_DIR" diff --name-only -- "src" "tests" "pyproject.toml" "README.md" "USAGE.md"
            git -C "$ROOT_DIR" diff --cached --name-only -- "src" "tests" "pyproject.toml" "README.md" "USAGE.md"
            git -C "$ROOT_DIR" ls-files --others --exclude-standard -- "src" "tests" "pyproject.toml" "README.md" "USAGE.md"
        } | sort -u
    )"

    if [ -n "$changed" ]; then
        warn "Git change report: source/docs-facing files have current worktree changes"
        printf '%s\n' "$changed" | sed 's/^/  - /'
    else
        pass "Git change report: source/docs-facing files unchanged"
    fi
}

say "[audit] repository: ${ROOT_DIR}"

check_file "deep-research-report.md" "source report"
check_file "docs/enterprise-tui/overview.md" "enterprise TUI overview"
check_file "docs/enterprise-tui/acceptance.md" "enterprise TUI acceptance"
check_file "docs/enterprise-tui/security-ops.md" "enterprise TUI security and ops"

check_contains "pyproject.toml" '^requires-python = ">=3\.11"$' "Python runtime stays >=3.11"
check_contains "pyproject.toml" '^tui = \["textual>=5\.0"\]$' "Textual optional dependency is declared"
check_contains "pyproject.toml" '^cproxy-tui = "cproxy\.tui\.app:run_tui"$' "cproxy-tui entry point is declared"

check_contains "docs/enterprise-tui/overview.md" '不做 Go/Bubble Tea 重写' "overview states no Go/Bubble Tea rewrite"
check_contains "docs/enterprise-tui/overview.md" 'Python 3\.11 \+ Textual' "overview states Python 3.11 + Textual"
check_contains "docs/enterprise-tui/overview.md" '已有能力' "overview lists existing capabilities"
check_contains "docs/enterprise-tui/overview.md" '企业缺口' "overview lists enterprise gaps"
check_contains "docs/enterprise-tui/acceptance.md" 'bash "scripts/audit-enterprise-tui\.sh"' "acceptance includes audit command"
check_contains "docs/enterprise-tui/security-ops.md" 'secret-systemd-credential|secret-file|keyring' "security docs call out secret provider state"

check_contains "src/cproxy/tui/app.py" 'class CProxyApp' "Textual app shell exists"
check_contains "src/cproxy/tui/app.py" 'TabbedContent' "Textual tabbed navigation exists"
check_contains "src/cproxy/backend/api.py" '127\.0\.0\.1:9090' "Controller defaults to loopback"
check_contains "src/cproxy/backend/api.py" 'Authorization.*Bearer' "Controller secret is sent as bearer token when configured"
check_contains "src/cproxy/backend/api.py" '/providers/proxies' "Proxy provider endpoint wrapper exists"
check_contains "src/cproxy/backend/api.py" '/connections' "Connection endpoint wrapper exists"
check_contains "src/cproxy/backend/process.py" 'ProcessOwnershipError' "Process ownership guard exists"
check_contains "src/cproxy/services/query.py" 'runtime\.get_groups' "Runtime fallback exists"
check_contains "src/cproxy/tui/screens/subscriptions.py" 'redact_subscription_url' "Subscription URL redaction exists"
check_file "src/cproxy/tui/screens/providers.py" "Provider TUI screen"
check_file "src/cproxy/tui/screens/connections.py" "Connections TUI screen"

check_file "tests/test_tui_app.py" "Textual app tests"
check_file "tests/test_tui_proxies.py" "proxy screen tests"
check_file "tests/test_tui_connections_providers.py" "provider and connection TUI tests"
check_file "tests/test_tui_subscriptions.py" "subscription TUI tests"
check_file "tests/test_tui_logs.py" "logs TUI tests"
check_file "tests/test_runtime_and_process.py" "runtime lifecycle tests"
check_file "tests/systemd_user_examples_test.sh" "systemd user example tests"

check_contains "src/cproxy" 'external-controller-tls' "external-controller-tls handling is implemented"
check_contains "src/cproxy" 'external-controller-unix' "external-controller-unix risk handling is implemented"
check_contains "src/cproxy" 'secret-systemd-credential|secret-file|CREDENTIALS_DIRECTORY' "secret provider is not plain YAML only"
check_contains "src/cproxy" 'secret-keyring-service|import_module\("keyring"\)' "keyring secret provider hook exists"
check_contains "src/cproxy" 'audit_event|cproxy-audit\.jsonl' "structured audit logging exists"
check_contains "src/cproxy" 'systemd-cat|audit-journald' "journald audit sink exists"
check_contains "src/cproxy" 'support-bundle|build_support_bundle|redact_value' "redacted support bundle exists"
check_contains "src/cproxy" 'security-check|validate_controller_security' "security-check CLI exists"
check_contains "tests" '80, 24|120, 32|5000' "GA layout and pressure tests exist"
check_file "scripts/build-ga-artifacts.sh" "GA artifact build script"
check_file "scripts/verify-ga-artifacts.sh" "GA artifact verification script"
check_contains ".github" 'scorecard|sbom|pip-audit|slsa|provenance' "supply-chain CI checks exist"

check_no_go_rewrite
report_git_scope

say "[audit] warnings: ${warnings}"
say "[audit] failures: ${failures}"

if [ "$failures" -gt 0 ]; then
    exit 1
fi

exit 0
