import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chuantuo import codex_cli
from chuantuo.models import ModelConfig, ModelError, ModelGateway, configured_key, measured_call
from chuantuo.profile import ModelProfile, ProfileInput


@pytest.fixture
def cli(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.touch()
    monkeypatch.setattr(codex_cli, "executable", lambda: str(binary))
    monkeypatch.setattr(codex_cli, "_supported", lambda *args: True)
    monkeypatch.setattr(codex_cli, "status", lambda: {"supported": True, "logged_in": True})
    return ModelConfig(provider="codex_cli", model="chosen-model", max_tokens=500, thinking="high")


def test_cli_context_and_environment_are_separate_per_call(cli, monkeypatch):
    calls = []
    monkeypatch.setenv("CODEX_APP_TOOLS_PIPE_PATH", "parent-tool-channel")
    monkeypatch.setenv("OPENAI_API_KEY", "api-secret-not-for-cli")
    monkeypatch.setenv("CODEX_HOME", "/local-auth-location")

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            self.answer = Path(args[args.index("--output-last-message") + 1])
            self.root = kwargs["cwd"]
            assert kwargs["env"]["CODEX_HOME"] == "/local-auth-location"
            assert "CODEX_APP_TOOLS_PIPE_PATH" not in kwargs["env"]
            assert "OPENAI_API_KEY" not in kwargs["env"]
            assert all(flag in args for flag in ("--ignore-user-config", "--ephemeral", "--sandbox"))
            assert args[args.index("--model") + 1] == "chosen-model"
            assert 'forced_login_method="chatgpt"' in args and "agents.enabled=false" in args
            assert "model_reasoning_effort=\"high\"" in args

        def communicate(self, prompt, timeout):
            assert timeout == cli.timeout_seconds
            system = (self.root / "instructions.md").read_text(encoding="utf-8")
            calls.append((self.root, system, prompt))
            self.answer.write_text(prompt, encoding="utf-8")
            return json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 42, "output_tokens": 7, "cached_input_tokens": 12,
            }}), ""

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    monkeypatch.setattr(codex_cli.subprocess, "Popen", Process)
    gateway = ModelGateway()
    receipts = []

    def ask(actor):
        return measured_call(gateway, cli, [
            {"role": "system", "content": "private instructions " + actor},
            {"role": "user", "content": actor},
        ], receipts.append)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(ask, ["actor-a", "actor-b"])) == ["actor-a", "actor-b"]
    assert len({root for root, _, _ in calls}) == 2
    for root, system, actor in calls:
        assert actor in system and ("actor-b" if actor == "actor-a" else "actor-a") not in system
        assert not root.exists()
    assert all(r["usage"]["cache_read_input_tokens"] == 12 and r["transport"] == "codex_cli" for r in receipts)
    assert not gateway._processes
    gateway.close()


@pytest.mark.parametrize("failure", ["timeout", "unfinished", "tools"])
def test_cli_failure_does_not_accept_partial_text_or_leak_diagnostics(cli, monkeypatch, failure):
    processes = []

    class Process:
        returncode = None

        def __init__(self, args, **kwargs):
            self.answer = Path(args[args.index("--output-last-message") + 1])
            self.answer.write_text("partial answer", encoding="utf-8")
            processes.append(self)

        def communicate(self, prompt=None, timeout=None):
            if failure == "timeout" and self.returncode is None:
                raise subprocess.TimeoutExpired("codex", 1)
            self.returncode = 0
            events = [{"type": "turn.completed"}] if failure == "tools" else [{"type": "turn.failed"}]
            if failure == "tools":
                events.append({"type": "item.completed", "item": {"type": "command_execution"}})
            return "\n".join(json.dumps(e) for e in events), "private CLI diagnostic"

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -1

        def wait(self):
            return self.returncode

    monkeypatch.setattr(codex_cli.subprocess, "Popen", Process)
    gateway = ModelGateway()
    receipts = []
    with pytest.raises(ModelError) as error:
        measured_call(gateway, cli, [{"role": "user", "content": "question"}], receipts.append)
    assert "private CLI diagnostic" not in str(error.value) + str(receipts)
    assert receipts[0]["error"] == {"timeout": "cli_timeout", "unfinished": "cli_failed", "tools": "cli_tools"}[failure]
    assert not processes[0].answer.parent.exists() and not gateway._processes
    gateway.close()
    with pytest.raises(ModelError, match="已关闭"):
        gateway.complete(cli, [{"role": "user", "content": "question"}])
    assert not gateway._processes


def test_switching_to_subscription_keeps_previous_config_and_key(cli, tmp_path):
    profile = ModelProfile(tmp_path)
    original = ModelConfig(provider="deepseek", model="saved-model", thinking="disabled")
    first = profile.save(ProfileInput(model=original, key="fixture-secret-to-retain"))
    retained = profile.model
    changed = profile.save(ProfileInput(model=cli))
    assert changed["ready"] and changed["previous_model"] == {"provider": "deepseek", "model": "saved-model"}
    assert "fixture-secret-to-retain" not in str(changed)
    restored = profile.restore_previous()
    assert profile.model == retained and restored["ready"]
    assert configured_key(profile.model) == "fixture-secret-to-retain"
    assert restored["previous_model"] == {"provider": "codex_cli", "model": "chosen-model"}
    assert profile.restore_previous()["model"]["provider"] == "codex_cli"
    assert first["model"]["provider"] == "deepseek"
