from workstation_bootstrap import gpu


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_ensure_nvidia_runtime_skips_without_gpu(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda _name: None)

    called: list[list[str]] = []
    monkeypatch.setattr(gpu.command, "run", lambda cmd, **kw: called.append(list(cmd)) or _Result())

    gpu.ensure_nvidia_runtime(dry_run=False)

    # nvidia-smi is absent, so nothing beyond detection should run.
    assert called == []


def test_ensure_nvidia_runtime_noop_when_already_configured(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda _name: "/usr/bin/" + _name)

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["nvidia-smi", "-L"]:
            return _Result(returncode=0, stdout="GPU 0: NVIDIA Test")
        if "grep" in cmd:
            return _Result(returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gpu.command, "run", fake_run)

    gpu.ensure_nvidia_runtime(dry_run=False)


def test_ensure_nvidia_runtime_installs_and_restarts(monkeypatch):
    # GPU present, toolkit absent (no nvidia-container-runtime), runtime missing from containerd.
    def fake_which(name):
        return None if name == "nvidia-container-runtime" else "/usr/bin/" + name

    monkeypatch.setattr(gpu.shutil, "which", fake_which)

    grep_returncode = {"value": 1}
    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        if cmd[:2] == ["nvidia-smi", "-L"]:
            return _Result(returncode=0, stdout="GPU 0: NVIDIA Test")
        if "grep" in cmd:
            return _Result(returncode=grep_returncode["value"])
        if cmd[:2] == ["curl", "-fsSL"]:
            return _Result(returncode=0, stdout="deb https://example/ /")
        return _Result(returncode=0)

    monkeypatch.setattr(gpu.command, "run", fake_run)
    monkeypatch.setattr(gpu.k3s, "is_running", lambda: True)

    restarted = {"value": False}

    def fake_restart():
        restarted["value"] = True

    monkeypatch.setattr(gpu.k3s, "restart", fake_restart)

    gpu.ensure_nvidia_runtime(dry_run=False)

    joined = [" ".join(cmd) for cmd in ran]
    assert any("apt-get install -y nvidia-container-toolkit" in c for c in joined)
    assert any("gpg --dearmor" in c for c in joined)
    assert restarted["value"] is True


def test_ensure_nvidia_runtime_dry_run_installs_nothing(monkeypatch):
    def fake_which(name):
        return None if name == "nvidia-container-runtime" else "/usr/bin/" + name

    monkeypatch.setattr(gpu.shutil, "which", fake_which)

    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ran.append(list(cmd))
        if cmd[:2] == ["nvidia-smi", "-L"]:
            return _Result(returncode=0, stdout="GPU 0: NVIDIA Test")
        if "grep" in cmd:
            return _Result(returncode=1)
        raise AssertionError(f"unexpected command in dry run: {cmd}")

    monkeypatch.setattr(gpu.command, "run", fake_run)

    gpu.ensure_nvidia_runtime(dry_run=True)

    joined = [" ".join(cmd) for cmd in ran]
    assert not any("apt-get" in c for c in joined)


def test_phase_gpu_runtime_registered_after_k3s():
    from workstation_bootstrap import phases

    names = [p.name for p in phases.PHASE_PLAN]
    assert names.index("gpu-runtime") == names.index("k3s") + 1
