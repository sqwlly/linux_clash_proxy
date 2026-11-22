"""针对 CLI / Rendering / Completion / Interactive / Scoped Services 审计修复的回归测试。"""

from __future__ import annotations

import base64
import http.client
import json
import os
import stat
from argparse import Namespace
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
import yaml

from cproxy import cli, cli_render
from cproxy.backend.models import (
    AIProbeReport,
    ConnectivityReport,
    GroupCheckReport,
    ProxyGroup,
)
from cproxy.completion import _candidates
from cproxy.config import AppPaths, config_file, default_paths, runtime_file
from cproxy.feedback import _truncate
from cproxy.interactive import _loop, _redraw, _terminal_read_key
from cproxy.logs import follow_lines, read_recent_lines
from cproxy.names import match_candidates_fuzzy
from cproxy.output import build_root_parser
from cproxy.services.benchmark import evaluate_providers
from cproxy.services.diagnostics import (
    PROBE_DEAD,
    DiagnosticsService,
    _headers_or_url_indicate_geo_blocked,
    _probe_target,
    _SafeRedirectHandler,
)
from cproxy.services.flush import flush_connections
from cproxy.services.node_tags import parse_node_tag
from cproxy.services.nodelist import match_region, parse_share_link, parse_subscription_payload
from cproxy.services.ops import AIConnection
from cproxy.services.prompt import PromptSnapshot, collect_prompt_snapshot, format_prompt
from cproxy.services.query import QueryService
from cproxy.services.refresh import ExtraSubscriptionResult, GroupSwitchResult, RefreshReport
from cproxy.services.speed import fetch_total_traffic, format_rate, measure_once
from cproxy.services.switch_tree import build_switch_tree, suggest_groups_from_names
from cproxy.services.verge import VergeEgressStatus, VergeStatusReport, VergeTickReport
from cproxy.services.why import _match_ruleset, _rule_target_group, explain_route, extract_host
from cproxy.textwidth import display_width


def _make_paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(
        config_dir=tmp_path / "cfg",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    return paths


# ---------------------------------------------------------------------------
# 1. Test isolation (POSIX HOME / CPROXY_*_DIR / _output_config cache)
# ---------------------------------------------------------------------------


def test_conftest_isolates_home_and_clears_output_config_cache(tmp_path: Path):
    assert Path.home() == tmp_path / "home"
    paths = default_paths()
    assert paths.config_dir.is_relative_to(tmp_path / "home")
    assert paths.data_dir.is_relative_to(tmp_path / "home")
    assert paths.state_dir.is_relative_to(tmp_path / "home")
    for var in ("CPROXY_CONFIG_DIR", "CPROXY_DATA_DIR", "CPROXY_STATE_DIR"):
        assert var not in os.environ
    assert cli_render._output_config() == {}


# ---------------------------------------------------------------------------
# 2. Diagnostics SSRF & protocol hardening
# ---------------------------------------------------------------------------


def test_diagnostics_rejects_non_http_urls_and_validates_exit_ip(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP_SECRET_DATA", encoding="utf-8")
    (paths.data_dir / "country.mmdb").write_bytes(b"mmdb")
    config_file(paths).write_text(
        yaml.safe_dump(
            {
                "mixed-port": 7890,
                "connectivity-timeout": -5,
                "connectivity-test-urls": [f"file://{secret}", "http://probe.local/ok"],
                "ip-check-urls": [
                    f"file://{secret}",
                    "http://probe.local/html",
                    "http://probe.local/ip",
                ],
            }
        ),
        encoding="utf-8",
    )

    opened: list[tuple[str, int]] = []
    read_limits: list[int | None] = []

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n: int | None = None) -> bytes:
            read_limits.append(n)
            return self._body if n is None else self._body[:n]

    class _Opener:
        def open(self, url: str, timeout: int = 5):
            opened.append((url, timeout))
            if url.endswith("/html"):
                return _Resp(b"<html>Cloudflare Challenge</html>")
            if url.endswith("/ip"):
                return _Resp(b"203.0.113.42\n")
            return _Resp(b"ok")

    svc = DiagnosticsService(paths)
    monkeypatch.setattr(svc.process, "is_running", lambda: True)
    monkeypatch.setattr("cproxy.services.diagnostics._proxy_opener", lambda _p: _Opener())

    report = svc.run_connectivity_test()

    # file:// 绝不能进入 opener.open，且负超时被钳到 >= 1
    assert all(url.startswith("http://") for url, _ in opened)
    assert all(t >= 1 for _, t in opened)
    assert read_limits == [65536, 256, 256]
    assert report.exit_ip == "203.0.113.42"
    file_res = next(r for r in report.results if r.name.startswith("file://"))
    assert file_res.ok is False

    # _probe_target 同样拒绝 file://
    verdict, detail = _probe_target(_Opener(), f"file://{secret}", 3, 0)
    assert verdict == PROBE_DEAD
    assert "URL" in detail

    # _SafeRedirectHandler 阻止重定向到 file://
    handler = _SafeRedirectHandler()
    with pytest.raises(URLError):
        handler.redirect_request(None, None, 302, "Found", {}, f"file://{secret}")


def test_diagnostics_probe_handles_http_exception_closes_http_error_and_geo_hosts():
    closed = []

    class _CloseableHTTPError(HTTPError):
        def __init__(self):
            super().__init__("https://example.com", 403, "Forbidden", Message(), None)

        def read(self, amt=None):
            return b"need api key"

        def close(self):
            closed.append(True)

    class _BadStatusOpener:
        def open(self, req, timeout=5):
            raise http.client.BadStatusLine("garbage")

    class _HttpErrOpener:
        def open(self, req, timeout=5):
            raise _CloseableHTTPError()

    assert _probe_target(_BadStatusOpener(), "https://example.com", 3, 0)[0] == PROBE_DEAD
    assert _probe_target(_HttpErrOpener(), "https://example.com", 3, 0)[0] == "ok"
    assert closed == [True]

    # google.com.hk / google.cn 域名与 Location 头判定地区门禁，且容忍不可调用的 headers.get
    assert _headers_or_url_indicate_geo_blocked(None, "https://www.google.com.hk/?gws_rd=ssl") is True
    assert _headers_or_url_indicate_geo_blocked(None, "https://google.cn/search") is True
    assert _headers_or_url_indicate_geo_blocked({"Location": "https://www.google.com.hk/"}, "") is True
    assert _headers_or_url_indicate_geo_blocked(SimpleNamespace(get="not-callable"), "https://www.google.com/") is False


def test_diagnostics_test_group_resolves_alias_handles_null_url_and_sentinel_delays(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    config_file(paths).write_text("test-url: null\ntest-timeout: 0\n", encoding="utf-8")
    seen_calls: list[tuple[str, str, int]] = []

    class _FakeAPI:
        def get_groups(self):
            return {
                "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "n1", ["n_ok", "n_zero", "n_timeout", "n_none"]),
            }

        def delay_test(self, name: str, url: str, timeout: int):
            seen_calls.append((name, url, timeout))
            return {
                "n_ok": {"delay": 120},
                "n_zero": {"delay": 0},
                "n_timeout": {"delay": 5000},
                "n_none": {"delay": None},
            }[name]

    svc = DiagnosticsService(paths)
    monkeypatch.setattr(svc, "api", _FakeAPI())

    progress_events: list[tuple[int, int, str, int | None]] = []
    report = svc.test_group("ai", on_progress=lambda *a: progress_events.append(a))
    assert report.group_name == "AI-MANUAL"
    assert all(url.startswith("https://") and url != "None" for _, url, _ in seen_calls)
    assert all(t >= 1 for _, _, t in seen_calls)
    assert [(r.name, r.ok) for r in report.results] == [
        ("n_ok", True),
        ("n_zero", False),
        ("n_timeout", False),
        ("n_none", False),
    ]
    assert [delay for _, _, _, delay in progress_events] == [120, None, None, None]


# ---------------------------------------------------------------------------
# 3. Why path traversal & rule matching
# ---------------------------------------------------------------------------


def test_why_blocks_ruleset_path_traversal_and_supports_domain_and_ipcidr(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    ruleset_dir = paths.data_dir / "ruleset"
    ruleset_dir.mkdir(parents=True)
    # 逃逸目标文件（若存在遍历会被读取）
    (paths.data_dir / "escape.yaml").write_text("payload:\n  - DOMAIN,secret.example\n", encoding="utf-8")
    assert _match_ruleset(paths, "../escape", "secret.example", None) is False
    assert _match_ruleset(paths, r"..\escape", "secret.example", None) is False
    assert (
        _match_ruleset(
            paths,
            "evil",
            "secret.example",
            None,
            rule_providers={"evil": {"behavior": "classical", "path": "../escape.yaml"}},
        )
        is False
    )

    # 合法 domain 与 ipcidr behavior
    (ruleset_dir / "cn_domains.yaml").write_text(
        "payload:\n  - '+.baidu.com'\n  - '.sub.example.org'\n  - '*.one.test'\n  - 'exact.test'\n",
        encoding="utf-8",
    )
    (ruleset_dir / "lan_ips.yaml").write_text(
        "payload:\n  - '114.114.114.0/24'\n",
        encoding="utf-8",
    )
    runtime_cfg = {
        "rule-providers": {
            "cn_domains": {"type": "file", "behavior": "domain", "path": "./ruleset/cn_domains.yaml"},
            "lan_ips": {"type": "file", "behavior": "ipcidr", "path": "./ruleset/lan_ips.yaml"},
        },
        "rules": [
            "RULE-SET,cn_domains,DIRECT",
            "RULE-SET,lan_ips,DIRECT",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve,src",
            "PROCESS-NAME,agy.exe,AI-MANUAL",
            "MATCH,Proxy",
        ],
    }
    runtime_file(paths).write_text(yaml.safe_dump(runtime_cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr("cproxy.services.why.APIBackend.get_groups", lambda self, **kw: {})

    assert explain_route(paths, "www.baidu.com").policy_group == "DIRECT"
    assert explain_route(paths, "114.114.114.114").policy_group == "DIRECT"
    assert explain_route(paths, "10.1.2.3").policy_group == "DIRECT"
    assert explain_route(paths, "example.com", process=r"C:\Users\me\agy.exe").policy_group == "AI-MANUAL"
    assert _rule_target_group("IP-CIDR,10.0.0.0/8,DIRECT,no-resolve,src") == "DIRECT"
    assert extract_host("example.com?a=1&b=2") == "example.com"
    assert extract_host("user:pass@example.com:443") == "example.com"
    assert extract_host("example.com#section") == "example.com"


def test_why_guards_corrupt_yaml_and_malformed_connections(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    runtime_file(paths).write_text(":::bad_yaml: [", encoding="utf-8")
    monkeypatch.setattr("cproxy.services.why.APIBackend.get_groups", lambda self, **kw: {})
    monkeypatch.setattr(
        "cproxy.services.why.APIBackend.get_connections",
        lambda self: {
            "connections": [
                {
                    "metadata": {"host": "example.com"},
                    "chains": "not-a-list",
                    "download": "not-an-int",
                    "upload": None,
                }
            ]
        },
    )
    rep = explain_route(paths, "example.com")
    assert rep.policy_group == "DIRECT"
    assert rep.active_connections[0].chain == []
    assert rep.active_connections[0].download == 0


# ---------------------------------------------------------------------------
# 4. CLI Rendering & Contract Fixes
# ---------------------------------------------------------------------------


def test_cli_render_empty_tables_colored_diff_cjk_alignment_and_paths(monkeypatch, tmp_path: Path, capsys):
    # _table_widths 空行不抛 TypeError
    assert cli_render._table_widths(["池", "状态"], []) == [2, 4]

    # _render_ai_status 在空组 + 空 probe 结果下不崩溃且标记未知/ok=False
    monkeypatch.setattr(cli_render, "run_ai_probe", lambda _p: AIProbeReport(results=[]))
    monkeypatch.setattr(cli_render, "get_active_ai_processes", lambda _p: [])
    assert cli_render._render_ai_status({}, raw=False) == 0
    assert "未知" in capsys.readouterr().out
    assert cli_render._render_ai_status({}, raw=False, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["probe_status"] == "未知"

    # 空 results 的 group_check 与 connectivity_report 返回 1
    assert cli_render._render_group_check(GroupCheckReport("G1", []), raw=False, json_output=True) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert cli_render._render_connectivity_report(ConnectivityReport([], None), json_output=True) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False

    # _render_colored_diff 遵守 CPROXY_COLOR=never
    monkeypatch.setenv("CPROXY_COLOR", "never")
    cli_render._render_colored_diff(["--- a\n", "+++ b\n", "@@ -1 +1 @@\n", "-old\n", "+new\n"])
    diff_out = capsys.readouterr().out
    assert "\x1b[" not in diff_out

    # "已启用" 属于正面状态
    monkeypatch.setenv("CPROXY_COLOR", "always")
    assert "\x1b[32m" in cli_render._status_label("已启用")

    # Windows 路径压缩
    short = cli_render._shorten_path(r"C:\Users\alice\AppData\Local\Programs\node_modules\vendor\mytool.exe", 28)
    assert short.startswith("…\\")
    assert display_width(short) <= 28


def test_cli_render_export_permissions_yaml_format_rule_remove_raw_and_refresh_exit(tmp_path: Path, capsys):
    paths = _make_paths(tmp_path)
    config_file(paths).write_text("mixed-port: 7890\nmax-ai-multiplier: 2.0\n", encoding="utf-8")

    # export -o out.yaml --json: 文件写 YAML (0600)，stdout 写 JSON 信封
    out_yaml = tmp_path / "bundle.yaml"
    assert cli_render._run_export(paths, output=str(out_yaml), as_json=True) == 0
    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["command"] == "export"
    raw_file_text = out_yaml.read_text(encoding="utf-8")
    assert not raw_file_text.lstrip().startswith("{")
    assert yaml.safe_load(raw_file_text)["bundle_version"] == 1
    if os.name != "nt":
        assert stat.S_IMODE(out_yaml.stat().st_mode) == 0o600

    # rule remove --raw
    assert cli_render._run_rule(paths, "add", "perplexity.ai", "ai", raw=True) == 0
    assert "RULE_ADDED\t" in capsys.readouterr().out
    assert cli_render._run_rule(paths, "remove", "perplexity.ai", raw=True) == 0
    assert "RULE_REMOVED\tDOMAIN-SUFFIX,perplexity.ai,AI-MANUAL" in capsys.readouterr().out

    # refresh 失败在 human / raw / json 三种模式下一致返回 1
    failed_report = RefreshReport(
        subscription="失败",
        subscription_detail="timeout",
        extra_subscriptions=[ExtraSubscriptionResult(name="SubB", status="失败", detail="502")],
        groups=[GroupSwitchResult(group="G1", current=None, action="失败", detail="missing")],
    )
    assert cli_render._render_refresh(failed_report, raw=False, json_output=False) == 1
    capsys.readouterr()
    assert cli_render._render_refresh(failed_report, raw=True, json_output=False) == 1
    capsys.readouterr()
    assert cli_render._render_refresh(failed_report, raw=False, json_output=True) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_verge_json_and_raw_modes_delay_sentinel_args_and_interrupt(tmp_path: Path, monkeypatch, capsys):
    paths = _make_paths(tmp_path)
    monkeypatch.setattr(cli, "default_paths", lambda: paths)

    # _print_verge_status 把 5000ms 显示为「超时」而非 5000 ms
    rep = VergeStatusReport(
        overall_effective=False,
        controller="127.0.0.1:9097",
        mixed_port=7897,
        controller_ok=True,
        egress=[VergeEgressStatus(group="AI-MANUAL", present=True, leaf_node="N1", delay_ms=5000, last_verdict="dead")],
        remedies=["fix me"],
    )
    cli._print_verge_status(rep)
    verge_out = capsys.readouterr().out
    assert "延迟 超时" in verge_out
    assert "5000 ms" not in verge_out

    # verge status --json 传递 ok=overall_effective
    monkeypatch.setattr("cproxy.services.verge.check_verge_effectiveness", lambda _p: rep)
    assert cli.run(["verge", "status", "--json"]) == 1
    verge_json = json.loads(capsys.readouterr().out)
    assert verge_json["ok"] is False

    # verge daemon --json 输出 JSONL
    class _FakeDaemon:
        def __init__(self, _p):
            pass

        def run_daemon(self, *, on_tick=None, **_kw):
            if on_tick is not None:
                on_tick(VergeTickReport(timestamp=1.0, sampled_connections=2))
                on_tick(VergeTickReport(timestamp=2.0, sampled_connections=3, warnings=["warn1"]))
            return 0

    monkeypatch.setattr("cproxy.services.verge.VergeDaemonService", _FakeDaemon)
    assert cli.run(["verge", "daemon", "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["ok"] is True
    assert lines[1]["ok"] is False

    # ai-rebalance / ai-watch / h2-keepalive 支持 --raw
    monkeypatch.setattr(
        cli.RebalanceService,
        "rebalance",
        lambda self, **_kw: SimpleNamespace(
            ordered=["N1"],
            gemini_ordered=["G1"],
            rerendered=False,
            hot_reloaded=False,
            switched_to="N1",
            gemini_switched_to=None,
            order_path=None,
            gemini_order_path=None,
        ),
    )
    assert cli.run(["ai-rebalance", "--raw"]) == 0
    reb_out = capsys.readouterr().out
    assert "REBALANCE\tmanual=1\tgemini=1" in reb_out
    assert "MANUAL_NODE\t1\tN1" in reb_out

    monkeypatch.setattr(
        cli.AIWatchService,
        "watch",
        lambda self, **_kw: SimpleNamespace(
            results=[SimpleNamespace(group="AI-MANUAL", verdict="ok", current="N1", target=None, switched=False)],
            reaped_pids=[1234],
        ),
    )
    assert cli.run(["ai-watch", "--raw"]) == 0
    watch_out = capsys.readouterr().out
    assert "WATCH\tAI-MANUAL\tok\tN1\t-\tswitched=false" in watch_out
    assert "REAPED_PIDS\t1234" in watch_out

    monkeypatch.setattr("cproxy.services.h2_keepalive.ensure_h2_keepalive_daemon", lambda *a, **kw: True)
    assert cli.run(["h2-keepalive", "--daemon", "--raw"]) == 0
    assert "H2_KEEPALIVE\tok\t127.0.0.1:7895" in capsys.readouterr().out

    # invalid float value 中文报错
    parser = build_root_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["speed", "--interval", "not-a-float"])
    assert exc.value.code == 2
    assert "需要数字" in capsys.readouterr().err

    # main() 优雅处理 KeyboardInterrupt 返回 130
    monkeypatch.setattr(cli, "run", lambda _argv: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit) as kb_exc:
        cli.main()
    assert kb_exc.value.code == 130


def test_logs_textwidth_feedback_and_names_edge_cases(tmp_path: Path, monkeypatch):
    log_path = tmp_path / "cproxy.log"
    log_path.write_bytes(b"line1\n\xff\xfeinvalid\nline3\n")
    assert read_recent_lines(log_path, 0) == []
    assert read_recent_lines(log_path, -3) == []
    lines = read_recent_lines(log_path, 2)
    assert len(lines) == 2 and lines[1] == "line3"

    # follow_lines 检测 copytruncate 截断与文件轮转
    step = 0

    def _on_sleep(_sec: float) -> None:
        nonlocal step
        step += 1
        if step == 1:
            log_path.write_text("truncated-line\n", encoding="utf-8")
        elif step == 2:
            log_path.rename(tmp_path / "cproxy.log.1")
            log_path.write_text("rotated-new-line\n", encoding="utf-8")
        else:
            raise AssertionError("follow_lines did not detect truncation/rotation")

    monkeypatch.setattr("cproxy.logs.time.sleep", _on_sleep)
    gen = follow_lines(log_path)
    assert next(gen) == "truncated-line"
    assert next(gen) == "rotated-new-line"
    gen.close()

    # textwidth 忽略 Mn/Me/Cf 零宽字符
    assert display_width("a\ufe0f\u200d\u200bb") == 2

    # feedback._truncate 不拆分国旗且正确处理零宽字符
    assert _truncate("🇺🇸abc", 3) == "🇺🇸…"
    assert _truncate("🇺🇸abc", 2) == "…"
    assert _truncate("a\ufe0fbc", 2) == "a\ufe0f…"

    # names.match_candidates_fuzzy 不被 Unicode 上标数字崩掉
    assert match_candidates_fuzzy("²", ["🇰🇷韩国 01"]) == []


# ---------------------------------------------------------------------------
# 5. Completion & Interactive Selector
# ---------------------------------------------------------------------------


def test_completion_fallback_ai_groups_flags_display_names_and_shells(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    groups = [
        ProxyGroup("AI-MANUAL", "Fallback", "KR-01", ["KR-01", "US-01"]),
        ProxyGroup("AI-GEMINI", "Fallback", "GB-01", ["GB-01"]),
        ProxyGroup("AI-US", "Fallback", "US-01", ["US-01"]),
        ProxyGroup("Proxy", "Selector", "HK-01", ["HK-01"]),
    ]
    monkeypatch.setattr("cproxy.completion.QueryService.list_groups", lambda self, **kw: list(groups))
    monkeypatch.setattr("cproxy.completion.QueryService.match_rule_group", lambda self: "Proxy")

    sw_groups = _candidates(paths, ["cproxy", "switch", ""], 2)
    assert "AI-MANUAL" in sw_groups
    assert "AI-GEMINI" in sw_groups
    assert "AI 出口" in sw_groups
    assert "AI-US" not in sw_groups

    # flags before positionals
    assert "AI-MANUAL" in _candidates(paths, ["cproxy", "current", "--json", ""], 3)
    assert "codex" in _candidates(paths, ["cproxy", "ai-use", "--group", "AI-MANUAL", ""], 4)
    assert "KR-01" in _candidates(paths, ["cproxy", "ban", "--hours", "2", ""], 4)
    # --ip 1.1.1.1 不能被误当作分组名
    assert "KR-01" in _candidates(paths, ["cproxy", "ip-check", "--ip", "1.1.1.1", "--node", ""], 5)
    # 别名与显示名能正确解析节点候选
    assert _candidates(paths, ["cproxy", "switch", "AI 出口", ""], 3) == ["KR-01", "US-01"]
    assert _candidates(paths, ["cproxy", "switch", "ai", ""], 3) == ["KR-01", "US-01"]
    # completion & flush all
    assert _candidates(paths, ["cproxy", "completion", ""], 2) == ["bash", "zsh"]
    assert "all" in _candidates(paths, ["cproxy", "flush", ""], 2)


def test_interactive_cjk_utf8_csi_drain_searching_indicator_and_truncation(monkeypatch):
    # 多字节 UTF-8 与多字节 CSI 序列（Delete = \x1b[3~）
    r_fd, w_fd = os.pipe()
    try:
        os.write(w_fd, "日".encode("utf-8") + b"\x1b[3~" + b"a")
        os.close(w_fd)
        w_fd = -1
        read_key = _terminal_read_key(r_fd)
        assert read_key() == "日"
        assert read_key() == "\x1b[3~"
        assert read_key() == "a"
        assert read_key() is None
    finally:
        os.close(r_fd)
        if w_fd != -1:
            os.close(w_fd)

    # 搜索模式下空结果时输入 j/k 应进入搜索词（不吞键），且刚按 / 即传 searching=True
    draws: list[tuple[list[str], str, bool]] = []
    stream = iter(["/", "z", "j", "\x7f", "\x7f", "j", "\r"])
    chosen = _loop(
        ["japan", "korea"],
        0,
        read_key=lambda: next(stream, None),
        draw=lambda filtered, _idx, q, searching: draws.append((list(filtered), q, searching)),
    )
    assert chosen == "japan"
    assert any(q == "" and searching is True for _, q, searching in draws)
    assert any(q == "zj" and searching is True for _, q, searching in draws)

    # _redraw 在 searching=True 且 query="" 时显示搜索栏，并截断超宽行
    writes: list[str] = []
    monkeypatch.setattr("cproxy.interactive._write", writes.append)
    monkeypatch.setattr("cproxy.interactive._visible_capacity", lambda **kw: 12)
    monkeypatch.setattr("cproxy.interactive._terminal_columns", lambda: 30)
    _redraw(["A" * 60], 0, "标题", 0, query="", searching=True)
    frame = "".join(writes)
    assert "搜索: ▏" in frame
    assert "…" in frame


# ---------------------------------------------------------------------------
# 6. Scoped Services
# ---------------------------------------------------------------------------


def test_flush_skips_keepalive_honors_all_stale_process_names_and_audits(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    runtime_file(paths).write_text("rules:\n  - MATCH,Proxy\n", encoding="utf-8")
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "KR-01", ["KR-01", "US-01"]),
        "Proxy": ProxyGroup("Proxy", "Selector", "HK-01", ["HK-01", "JP-01"]),
        "KR-01": ProxyGroup("KR-01", "VLESS", "-", []),
        "US-01": ProxyGroup("US-01", "VLESS", "-", []),
        "HK-01": ProxyGroup("HK-01", "VLESS", "-", []),
        "JP-01": ProxyGroup("JP-01", "VLESS", "-", []),
    }
    conns = [
        {"id": "c-h2", "chains": ["H2-KEEPALIVE", "AI-GEMINI"], "metadata": {"host": "a.com", "process": "agy"}},
        {"id": "c-stale", "chains": ["JP-01", "Proxy"], "metadata": {"host": "b.com", "process": "curl"}},
        {"id": "c-fresh", "chains": ["HK-01", "Proxy"], "metadata": {"host": "c.com", "process": "curl"}},
        {"id": "c-claude", "chains": ["KR-01", "AI-MANUAL"], "metadata": {"host": "api.anthropic.com", "process": "claude"}},
        {"id": "c-codex", "chains": ["KR-01", "AI-MANUAL"], "metadata": {"host": "chatgpt.com", "process": "codex"}},
        {"id": "c-nohost", "chains": "invalid", "metadata": {}},
    ]
    closed: list[str] = []
    monkeypatch.setattr("cproxy.services.flush.APIBackend.get_groups", lambda self, **kw: groups)
    monkeypatch.setattr("cproxy.services.flush.APIBackend.get_connections", lambda self: {"connections": conns})
    monkeypatch.setattr("cproxy.services.flush.APIBackend.close_connection", lambda self, cid: closed.append(cid) or True)

    # all --stale 仅关陈旧连接，不杀 H2-KEEPALIVE 与新鲜连接
    flush_connections(paths, "all", stale_only=True)
    assert closed == ["c-stale"]

    # flush claude 只关 claude 进程，不劫持整组 AI-MANUAL 把 codex 也关掉
    closed.clear()
    flush_connections(paths, "claude")
    assert closed == ["c-claude"]

    # flush default 解析到 match_group (Proxy)
    closed.clear()
    flush_connections(paths, "default")
    assert closed == ["c-stale", "c-fresh"]

    # flush - 不匹配占位 host "-"
    closed.clear()
    flush_connections(paths, "-")
    assert closed == []

    # 显式指定 H2-KEEPALIVE 时才关闭它
    closed.clear()
    flush_connections(paths, "H2-KEEPALIVE")
    assert closed == ["c-h2"]

    # 写审计日志
    audit_file = paths.state_dir / "cproxy-audit.jsonl"
    assert audit_file.exists()
    assert "flush_connections" in audit_file.read_text(encoding="utf-8")


def test_switch_tree_includes_gemini_guards_cycles_and_disambiguates_duplicates():
    groups = {
        "Proxy": ProxyGroup(
            "Proxy",
            "Selector",
            "自动选择",
            ["自动选择", "Auto", "SubA-HK", "SubB-HK", "香港 01", "日本 01", "EmptyInfo", "PoolA", "PoolB"],
        ),
        "自动选择": ProxyGroup("自动选择", "URLTest", "SubA-HK", ["SubA-HK"]),
        "Auto": ProxyGroup("Auto", "URLTest", "香港 01", ["香港 01"]),
        "SubA-HK": ProxyGroup("SubA-HK", "Selector", "HK-A1", ["HK-A1"]),
        "SubB-HK": ProxyGroup("SubB-HK", "Selector", "HK-B1", ["HK-B1"]),
        "EmptyInfo": ProxyGroup("EmptyInfo", "Selector", "剩余流量：10 GB", ["剩余流量：10 GB"]),
        "PoolA": ProxyGroup("PoolA", "Fallback", "PoolB", ["PoolB"]),
        "PoolB": ProxyGroup("PoolB", "Fallback", "PoolA", ["PoolA"]),
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "US-01", ["US-01"]),
        "AI-GEMINI": ProxyGroup("AI-GEMINI", "Fallback", "KR-01", ["KR-01"]),
    }
    entries = build_switch_tree(groups, match_group="Proxy", extra_names=["SubA", "SubB"])
    keys = [e.key for e in entries]
    assert "AI-GEMINI" in keys

    proxy_entry = next(e for e in entries if e.key == "Proxy")
    displays = [c.display for c in proxy_entry.children]
    # 无重复显示名，且 SubA-HK（被 URLTest 自动选择引用）未被隐藏，直连香港叶子未被丢弃
    assert len(displays) == len(set(displays))
    assert any("SubA-HK" in d for d in displays)
    assert any("SubB-HK" in d for d in displays)
    assert any("直连节点" in d for d in displays)
    # 互引用环 PoolA<->PoolB 至少保留一项
    assert any(c.key in {"PoolA", "PoolB"} for c in proxy_entry.children)
    # 全信息节点 selector 不可下钻且标注 0 个节点
    empty_choice = next(c for c in proxy_entry.children if c.key == "EmptyInfo")
    assert empty_choice.drillable is False
    assert empty_choice.annotation == "0 个节点"

    # 前缀匹配排在子串匹配前
    hint = suggest_groups_from_names("ai", ["My-AI-Group", "AI-MANUAL"])
    assert hint.index("AI-MANUAL") < hint.index("My-AI-Group")


def test_query_service_resolves_nested_keep_leaf_inner_to_outer_and_malformed_payload(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    groups = {
        "AI-MANUAL": ProxyGroup("AI-MANUAL", "Selector", "🇯🇵 Japan", ["🇯🇵 Japan", "🇺🇸 United States"]),
        "🇯🇵 Japan": ProxyGroup("🇯🇵 Japan", "Selector", "JP-01", ["JP-01", "JP-02"]),
        "JP-01": ProxyGroup("JP-01", "VLESS", "-", []),
        "JP-02": ProxyGroup("JP-02", "VLESS", "-", []),
    }
    calls: list[tuple[str, str]] = []
    closed: list[str] = []

    class _FakeAPI:
        def get_groups(self):
            return dict(groups)

        def switch_group(self, g: str, t: str):
            calls.append((g, t))
            old = groups[g]
            groups[g] = ProxyGroup(old.name, old.type, t, old.candidates, alive=old.alive, delay=old.delay)

        def get_connections(self):
            return {
                "connections": [
                    {"id": "old-jp1", "chains": ["JP-01", "🇯🇵 Japan", "AI-MANUAL"], "metadata": {"host": "openai.com"}},
                    {"id": "new-jp2", "chains": ["JP-02", "🇯🇵 Japan", "AI-MANUAL"], "metadata": {"host": "openai.com"}},
                ]
            }

        def close_connection(self, cid: str):
            closed.append(cid)

        def get_proxy_providers(self):
            return None

    svc = QueryService(paths)
    svc.api = _FakeAPI()  # type: ignore[assignment]

    # switch_path 先切内层再切外层，仅断一次旧连接并精准保留 JP-02
    svc.switch_path([("AI-MANUAL", "🇯🇵 Japan"), ("🇯🇵 Japan", "JP-02")])
    assert calls == [("🇯🇵 Japan", "JP-02"), ("AI-MANUAL", "🇯🇵 Japan")]
    assert closed == ["old-jp1"]

    # non-dict JSON 不抛 AttributeError
    assert svc.list_proxy_providers() == []


def test_nodelist_ipv6_ports_urlsafe_b64_plaintext_and_region_boundaries():
    name, vless = parse_share_link("  vless://11111111-2222-3333-4444-555555555555@[2001:db8::1]:443#V6-Node  ")
    assert name == "V6-Node"
    assert vless["server"] == "2001:db8::1"
    assert vless["port"] == 443

    with pytest.raises(ValueError):
        parse_share_link("trojan://pw@example.com:99999#BadPort")
    with pytest.raises(ValueError):
        parse_share_link("ss://aes-128-gcm:pw@[2001:db8::1]:0#ZeroPort")

    # 非 list proxies 报错
    with pytest.raises(ValueError, match="列表"):
        parse_subscription_payload(b"proxies: invalid-string\n")

    # URL-safe base64 与纯文本 URI 列表均支持
    uri = "trojan://pass@jp.example.com:443#JP-1"
    urlsafe_raw = base64.urlsafe_b64encode(uri.encode("utf-8")).rstrip(b"=")
    assert parse_subscription_payload(urlsafe_raw)["proxies"][0]["name"] == "JP-1"
    assert parse_subscription_payload(uri.encode("utf-8"))["proxies"][0]["name"] == "JP-1"

    # 地区边界：避免 English "in" / French "de" 误判，并兼容 CJK 紧邻
    assert match_region("Node in Netherlands 01") == "OTHER"
    assert match_region("Île de France 01") == "FR"
    assert match_region("节点UK 01") == "GB"
    assert match_region("节点US1") == "US"
    assert match_region("节点DE01") == "DE"
    assert match_region("节点IN01") == "IN"


def test_node_tags_cjk_adjacent_dedicated_indices_and_zero_multiplier():
    t1 = parse_node_tag("香港IEPL01")
    assert t1.line_type == "iepl" and t1.index == "01" and t1.is_dedicated_line is True

    t2 = parse_node_tag("香港CMI 02 | 0X")
    assert t2.carrier == "CM" and t2.multiplier == 0.0 and t2.index == "02"

    t3 = parse_node_tag("US-West-03 专线")
    assert t3.is_dedicated_line is True and t3.index == "03"

    assert parse_node_tag("US01").index == "01"
    assert parse_node_tag("Mitce US1-HY2").index == "01"
    assert parse_node_tag("香港 (01)").index == "01"
    assert parse_node_tag("🇭🇰香港 365天 01").index == "01"
    assert parse_node_tag("🇺🇸美国 100G 02").index == "02"
    assert parse_node_tag("🇯🇵日本 100M 03").index == "03"
    assert parse_node_tag("香港CN2 01").carrier == "CT"
    assert parse_node_tag("香港BGP 01").line_type == "bgp"


def test_prompt_direct_api_probe_yaml_fallback_and_tmux_escape(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    # 即使 ProcessBackend.is_running() 为 False（Windows/Verge），直连 API 成功时仍显示运行中
    monkeypatch.setattr("cproxy.services.prompt.ProcessBackend.is_running", lambda self: False)
    monkeypatch.setattr(
        "cproxy.services.prompt.APIBackend.get_groups",
        lambda self, **kw: {
            "AI-MANUAL": ProxyGroup("AI-MANUAL", "Fallback", "🇺🇸美国 01", ["🇺🇸美国 01"]),
            "🇺🇸美国 01": ProxyGroup("🇺🇸美国 01", "VLESS", "-", []),
        },
    )
    snap = collect_prompt_snapshot(paths)
    assert snap.running is True
    assert snap.ai_compact == "🇺🇸美国01"

    # API 失败但进程运行时回退 load_yaml_file
    monkeypatch.setattr(
        "cproxy.services.prompt.APIBackend.get_groups",
        lambda self, **kw: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr("cproxy.services.prompt.ProcessBackend.is_running", lambda self: True)
    runtime_file(paths).write_text(
        yaml.safe_dump(
            {"proxy-groups": [{"name": "AI-MANUAL", "type": "fallback", "proxies": ["🇯🇵日本 02"]}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    snap_rt = collect_prompt_snapshot(paths)
    assert snap_rt.running is True
    assert snap_rt.ai_compact == "🇯🇵日本02"

    # tmux 转义 '#'
    tmux_str = format_prompt(PromptSnapshot(running=True, proxy_env_active=False, ai_node="Node #2"), "tmux")
    assert "##2" in tmux_str


def test_benchmark_excludes_keepalive_longest_prefix_and_offline_distinction(tmp_path: Path, monkeypatch):
    paths = _make_paths(tmp_path)
    cfg = {
        "subscriptions": [
            {"name": "Sub", "url": "https://a.example"},
            {"name": "Sub Pro", "url": "https://b.example"},
        ],
        "proxies": [
            {"name": "H2-KEEPALIVE", "type": "http", "server": "127.0.0.1", "port": 7895},
            {"name": "🇺🇸美国 01", "type": "vless", "server": "1.1.1.1", "port": 443},
            {"name": "Sub HK 01", "type": "vless", "server": "1.1.1.2", "port": 443},
            {"name": "Sub Pro JP 01", "type": "vless", "server": "1.1.1.3", "port": 443},
        ],
    }
    config_file(paths).write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    # 离线时绝不伪造 100% alive
    from cproxy.backend.api import APIUnavailableError

    monkeypatch.setattr(
        "cproxy.services.benchmark.APIBackend.get_groups",
        lambda self, **kw: (_ for _ in ()).throw(APIUnavailableError("offline")),
    )
    offline_cards = {c.name: c for c in evaluate_providers(paths)}
    assert set(offline_cards) == {"主订阅", "Sub", "Sub Pro"}
    assert offline_cards["主订阅"].total_nodes == 1
    assert offline_cards["主订阅"].live_data is False
    assert offline_cards["主订阅"].alive_nodes == 0
    assert offline_cards["主订阅"].grade == "-"
    assert offline_cards["Sub Pro"].total_nodes == 1

    # 在线但节点未测速（delay=None, alive=True）记入 untested，不判死
    monkeypatch.setattr(
        "cproxy.services.benchmark.APIBackend.get_groups",
        lambda self, **kw: {
            "🇺🇸美国 01": ProxyGroup("🇺🇸美国 01", "VLESS", "-", [], delay=None, alive=True),
            "Sub HK 01": ProxyGroup("Sub HK 01", "VLESS", "-", [], delay=120, alive=True),
            "Sub Pro JP 01": ProxyGroup("Sub Pro JP 01", "VLESS", "-", [], delay=150, alive=True),
        },
    )
    online_cards = {c.name: c for c in evaluate_providers(paths)}
    assert online_cards["主订阅"].untested_nodes == 1
    assert online_cards["主订阅"].grade == "-"
    assert online_cards["Sub Pro"].alive_nodes == 1


def test_speed_guards_malformed_payload_negative_rate_and_invalid_window():
    assert format_rate(-42.0) == "0.0 B/s"
    assert format_rate(float("nan")) == "0.0 B/s"

    class _BadAPI:
        def get_connections(self):
            return {"uploadTotal": "bad", "downloadTotal": None}

    assert fetch_total_traffic(_BadAPI()) == (0, 0)
    assert fetch_total_traffic(SimpleNamespace(get_connections=lambda: None)) == (0, 0)

    with pytest.raises(ValueError):
        measure_once(_BadAPI(), window=0)
    with pytest.raises(ValueError):
        measure_once(_BadAPI(), window=-1.0)


def test_cli_speed_watch_tty_and_nontty_render_apply_deferred_and_cjk_headers(tmp_path: Path, monkeypatch, capsys):
    paths = _make_paths(tmp_path)
    monkeypatch.setattr(cli, "default_paths", lambda: paths)

    # speed --watch 非 TTY 不发 \r，按行输出
    samples = iter([(0, 0), (1024, 2048)])
    monkeypatch.setattr(cli, "fetch_total_traffic", lambda _api: next(samples))
    sleep_calls = 0

    def _fake_sleep(_sec: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", _fake_sleep)
    assert cli._run_speed(Namespace(watch=True, interval=0.5, raw=False)) == 0
    out_nontty = capsys.readouterr().out
    assert "\r" not in out_nontty
    assert "↓" in out_nontty and "↑" in out_nontty

    # render --apply 因活跃 AI 连接暂缓热重载时给出明确提示
    rt = runtime_file(paths)
    rt.write_text("rules:\n  - MATCH,DIRECT\n", encoding="utf-8")
    monkeypatch.setattr(cli, "render_runtime", lambda _p: rt)
    monkeypatch.setattr(cli.ProcessBackend, "is_running", lambda self: True)
    monkeypatch.setattr(cli.ProcessBackend, "is_runtime_stale", lambda self, _p=None: True)
    monkeypatch.setattr("cproxy.backend.runtime._mihomo_in_memory_using_stale_104", lambda _p, _s: False)
    monkeypatch.setattr("cproxy.services.ai_watch.has_any_inflight_ai_connection", lambda _p, _a: True)
    assert cli.run(["render", "--apply"]) == 0
    assert "暂缓热重载" in capsys.readouterr().out

    # _render_ai_connections CJK 表头宽度对齐
    monkeypatch.setenv("CPROXY_COLOR", "never")
    monkeypatch.setattr(
        cli_render,
        "get_ai_connections",
        lambda _p: [AIConnection(host="chatgpt.com", count=2, route="AI-MANUAL -> US-01")],
    )
    assert cli_render._render_ai_connections(paths, raw=False) == 0
    conn_lines = capsys.readouterr().out.splitlines()
    header_line = next(line for line in conn_lines if "目标域名" in line)
    row_line = next(line for line in conn_lines if "chatgpt.com" in line)
    assert display_width(header_line.split("出口链路")[0]) == display_width(row_line.split("AI-MANUAL")[0])
