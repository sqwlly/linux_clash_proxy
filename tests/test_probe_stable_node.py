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
    assert "NODE\t🇺🇸 United States丨01\tsuccess=1/1" in stdout
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
    assert "探测进度: 目标组 AI-MANUAL，候选 3 个，计划 3 轮" in stderr
    assert "第 1/3 轮" in stderr
    assert "United States 01 300ms" in stderr
    assert "United States 02 80ms" in stderr
    assert "Singapore 01 120ms" in stderr
    assert "完成，候选 3 个" in stderr
    assert " 33%" in stderr
    assert "100%" in stderr
    assert "探测进度: 下一轮保留 2 个，淘汰 1 个" in stderr
    assert "探测进度: 探测完成" in stderr
    assert "摘要" in stdout
    assert "推荐: United States 02" in stdout
