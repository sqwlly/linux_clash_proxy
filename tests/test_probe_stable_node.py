import copy
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


def _load_probe_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "probe_stable_node.py"
    script_dir = str(script.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("probe_stable_node_under_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_probe(monkeypatch, capsys, payload, delays, args):
    module = _load_probe_module()
    state_payload = copy.deepcopy(payload)
    puts = []

    def fake_request_json(controller, secret, path, method="GET", payload=None):
        parsed = urlsplit(path)
        if method == "GET" and parsed.path == "/proxies":
            return state_payload

        if method == "GET" and parsed.path.startswith("/proxies/") and parsed.path.endswith("/delay"):
            target = parsed.path[len("/proxies/") : -len("/delay")]
            target = unquote(target)
            if target in delays:
                return {"delay": delays[target]}
            raise RuntimeError(f"missing delay: {target}")

        if method == "PUT" and parsed.path.startswith("/proxies/"):
            group = unquote(parsed.path[len("/proxies/") :])
            target = payload["name"] if isinstance(payload, dict) else json.loads(payload).get("name")
            puts.append((group, target))
            group_state = state_payload["proxies"].get(group)
            if isinstance(group_state, dict):
                group_state["now"] = target
            return {}

        raise RuntimeError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(module, "request_json", fake_request_json)
    monkeypatch.setattr(sys, "argv", ["probe_stable_node.py", "--controller", "http://controller", *args])
    rc = module.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err, puts


def test_probe_stable_node_defaults_to_all_ai_leaf_nodes_and_switches_path(monkeypatch, capsys):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "🇯🇵 Japan",
                "all": ["AI-AUTO", "AI-US", "AI-SG", "🇯🇵 Japan", "🇺🇸 United States", "🇸🇬 Singapore"],
            },
            "AI-AUTO": {
                "type": "Fallback",
                "now": "AI-SG",
                "all": ["AI-US", "AI-SG"],
            },
            "AI-US": {
                "type": "Fallback",
                "now": "🇺🇸 United States丨01",
                "all": ["🇺🇸 United States丨01", "🇺🇸 United States丨02"],
            },
            "AI-SG": {
                "type": "Fallback",
                "now": "🇸🇬 Singapore丨01",
                "all": ["🇸🇬 Singapore丨01"],
            },
            "🇯🇵 Japan": {
                "type": "Selector",
                "now": "🇯🇵 Japan丨01",
                "all": ["🇯🇵 Japan丨01"],
            },
            "🇺🇸 United States": {
                "type": "Selector",
                "now": "🇺🇸 United States丨01",
                "all": ["🇺🇸 United States丨01", "🇺🇸 United States丨02"],
            },
            "🇸🇬 Singapore": {
                "type": "Selector",
                "now": "🇸🇬 Singapore丨01",
                "all": ["🇸🇬 Singapore丨01"],
            },
        }
    }

    delays = {
        "🇺🇸 United States丨01": 300,
        "🇺🇸 United States丨02": 80,
        "🇸🇬 Singapore丨01": 120,
        "🇯🇵 Japan丨01": 200,
    }
    rc, stdout, stderr, puts = _run_probe(
        monkeypatch,
        capsys,
        payload,
        delays,
        [
            "--profile",
            "chatgpt",
            "--strategy",
            "aggressive",
            "--rounds",
            "3",
            "--timeout",
            "1000",
            "--url",
            "http://probe.local/ping",
            "--raw",
            "--switch",
        ],
    )

    assert rc == 0, stderr
    assert "探测进度" not in stderr
    assert "GROUP\tAI-MANUAL" in stdout
    assert "CURRENT\t🇯🇵 Japan丨01" in stdout
    assert "BEST\t🇺🇸 United States丨02" in stdout
    assert "NODE\t🇺🇸 United States丨01\tsuccess=1/3" in stdout
    assert "NODE\t🇺🇸 United States丨02" in stdout
    assert "NODE\t🇸🇬 Singapore丨01" in stdout
    assert "NODE\t🇯🇵 Japan丨01" in stdout
    assert "NODE\tAI-AUTO" not in stdout
    assert "NODE\tAI-US" not in stdout
    assert "NODE\tAI-SG" not in stdout
    assert puts == [
        ("AI-US", "🇺🇸 United States丨02"),
        ("AI-AUTO", "AI-US"),
        ("AI-MANUAL", "AI-AUTO"),
    ]


def test_probe_stable_node_human_mode_reports_progress(monkeypatch, capsys):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "AI-AUTO",
                "all": ["AI-AUTO"],
            },
            "AI-AUTO": {
                "type": "Fallback",
                "now": "AI-US",
                "all": ["AI-US", "AI-SG"],
            },
            "AI-US": {
                "type": "Fallback",
                "now": "🇺🇸 United States丨02",
                "all": ["🇺🇸 United States丨01", "🇺🇸 United States丨02"],
            },
            "AI-SG": {
                "type": "Fallback",
                "now": "🇸🇬 Singapore丨01",
                "all": ["🇸🇬 Singapore丨01"],
            },
        }
    }

    delays = {
        "🇺🇸 United States丨01": 300,
        "🇺🇸 United States丨02": 80,
        "🇸🇬 Singapore丨01": 120,
    }
    rc, stdout, stderr, _puts = _run_probe(
        monkeypatch,
        capsys,
        payload,
        delays,
        [
            "--profile",
            "chatgpt",
            "--strategy",
            "aggressive",
            "--rounds",
            "3",
            "--timeout",
            "1000",
            "--url",
            "http://probe.local/ping",
        ],
    )

    assert rc == 0, stderr
    assert "探测 | AI-MANUAL | 3 节点 | 3 轮" in stderr
    assert "轮 1/3" in stderr
    assert "United States 01 300ms" not in stderr
    assert "United States 02 80ms" not in stderr
    assert "Singapore 01 120ms" not in stderr
    assert "完成，候选" not in stderr
    assert " 33%" in stderr
    assert "100%" in stderr
    assert "筛选 | 保留 2 | 淘汰 1" in stderr
    assert "完成 | 摘要如下" in stderr
    assert "摘要" in stdout
    assert "推荐: United States 02" in stdout
    assert "节点              成功  失败   平均   最大   最小  score" in stdout


def test_result_table_lines_are_column_aligned():
    module = _load_probe_module()
    summaries = [
        module.ProbeSummary("Japan 02", [184, 187, 178, 184, 187], 0),
        module.ProbeSummary("United States 03", [216, 217], 0),
        module.ProbeSummary("Singapore 04", [355], 0),
    ]

    assert module.result_table_lines(summaries, 5, module.STRATEGIES["conservative"]) == [
        "节点              成功  失败   平均   最大   最小  score",
        "Japan 02           5/5     0  184ms  187ms  178ms     97",
        "United States 03   2/5     0  216ms  217ms  216ms     36",
        "Singapore 04       1/5     0  355ms  355ms  355ms     13",
    ]


def test_result_table_scores_early_eliminated_nodes_against_requested_rounds():
    module = _load_probe_module()
    summaries = [
        module.ProbeSummary("Full", [100, 100, 100, 100, 100], 0),
        module.ProbeSummary("Early", [80], 0),
    ]

    assert module.result_table_lines(summaries, 5, module.STRATEGIES["conservative"]) == [
        "节点              成功  失败   平均   最大   最小  score",
        "Full               5/5     0  100ms  100ms  100ms     98",
        "Early              1/5     0   80ms   80ms   80ms     19",
    ]


def test_history_penalty_demotes_recently_unstable_node(monkeypatch, capsys, tmp_path):
    history_file = tmp_path / "history.jsonl"
    history_file.write_text(
        json.dumps(
            {
                "profile": "chatgpt",
                "group": "AI-MANUAL",
                "url": "http://probe.local/ping",
                "rounds": 3,
                "nodes": [
                    {"name": "Fast", "success": 0, "failures": 3},
                    {"name": "Clean", "success": 3, "failures": 0},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "Clean",
                "all": ["Fast", "Clean"],
            },
        }
    }

    rc, stdout, stderr, _puts = _run_probe(
        monkeypatch,
        capsys,
        payload,
        {"Fast": 80, "Clean": 90},
        [
            "--profile",
            "chatgpt",
            "--strategy",
            "aggressive",
            "--rounds",
            "3",
            "--timeout",
            "1000",
            "--url",
            "http://probe.local/ping",
            "--history-file",
            str(history_file),
            "--raw",
        ],
    )

    assert rc == 0, stderr
    assert "BEST\tClean" in stdout
    assert "NODE\tFast\tsuccess=1/3" in stdout


def test_human_mode_previews_switch_skip_reason(monkeypatch, capsys):
    payload = {
        "proxies": {
            "AI-MANUAL": {
                "type": "Selector",
                "now": "Current",
                "all": ["Current", "Better"],
            },
        }
    }

    rc, stdout, stderr, _puts = _run_probe(
        monkeypatch,
        capsys,
        payload,
        {"Current": 100, "Better": 80},
        [
            "--profile",
            "chatgpt",
            "--strategy",
            "aggressive",
            "--rounds",
            "3",
            "--timeout",
            "1000",
            "--url",
            "http://probe.local/ping",
        ],
    )

    assert rc == 0, stderr
    assert "推荐: Better" in stdout
    assert "切换预览: 不会切换 (当前节点也稳定，平均延迟改善 20ms 未达到 50ms 防抖门槛)" in stdout
