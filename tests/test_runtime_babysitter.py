import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeBabysitterTests(unittest.TestCase):
    def test_runtime_supervisor_is_singleton_and_owns_core_children(self):
        text = (ROOT / "scripts" / "worker_launcher.ps1").read_text(encoding="utf-8")
        self.assertIn("Local\\LifeOSRuntimeSupervisor", text)
        self.assertIn("AbandonedMutexException", text)
        self.assertIn("$ownsMutex = $true", text)
        self.assertIn("LIFE OS worker", text)
        self.assertIn("LIFE OS Control Room", text)
        self.assertIn("MONOLITH Windows Control Agent", text)
        self.assertIn("windows-control", text)
        self.assertIn("desktop_commander_guardian.ps1", text)
        self.assertIn("optional fallback", text)
        self.assertIn("Start-Child", text)

    def test_native_control_has_independent_startup_and_desktop_commander_is_optional(self):
        launcher = (ROOT / "scripts" / "native_control_launcher.ps1").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install_native_control_agent.ps1").read_text(encoding="utf-8")
        worker_install = (ROOT / "scripts" / "install_worker.ps1").read_text(encoding="utf-8")
        self.assertIn("Local\\LifeOSNativeWindowsControlLauncher", launcher)
        self.assertIn("windows-control", launcher)
        self.assertIn("LIFE OS Native Windows Control", installer)
        self.assertIn("HKCU:", installer)
        self.assertIn("install_native_control_agent.ps1", worker_install)
        self.assertIn("native Windows control install failed", worker_install)
        self.assertIn("optional-fallback-install-failed", worker_install)
        self.assertNotIn('throw "Desktop Commander recovery installer is missing"', worker_install)

    def test_desktop_commander_guardian_is_independent_and_bounded(self):
        text = (ROOT / "scripts" / "desktop_commander_guardian.ps1").read_text(encoding="utf-8")
        self.assertIn("Local\\LifeOSDesktopCommanderGuardian", text)
        self.assertIn("AbandonedMutexException", text)
        self.assertIn("$ownsMutex = $true", text)
        self.assertIn("@wonderwhy-er\\desktop-commander\\dist\\index.js", text)
        self.assertIn("npm-cache\\_npx", text)
        self.assertIn("Get-NetTCPConnection", text)
        self.assertIn("Device marked as offline", text)
        self.assertIn("Remote session expired", text)
        self.assertIn("remote transport disconnected", text)
        self.assertIn("Stop-OwnedTree $process.Id", text)
        self.assertNotIn("npm install", text.lower())
        self.assertNotIn("npx @wonderwhy-er", text.lower())
        self.assertNotIn("function Write-State([string]$Status, [int]$Pid", text)

    def test_guardian_migrates_only_exact_legacy_desktop_commander_route(self):
        text = (ROOT / "scripts" / "desktop_commander_guardian.ps1").read_text(encoding="utf-8")
        self.assertIn("desktop-commander-guardian-migrated-v1", text)
        self.assertIn("LIFE OS Desktop Commander Supervisor", text)
        self.assertIn("Get-ItemProperty -Path $runKey", text)
        self.assertNotIn("Get-ItemPropertyValue -Path $runKey -Name $legacyRunName", text)
        self.assertIn("desktop_commander_supervisor\\.ps1", text)
        self.assertIn("legacySupervisorPath", text)
        self.assertIn("retiring legacy Desktop Commander supervisor", text)
        self.assertIn("Stop-OwnedTree ([int]$legacySupervisor.ProcessId)", text)
        self.assertIn("@wonderwhy-er[\\\\/]desktop-commander", text)
        self.assertIn("Name='node.exe'", text)
        self.assertNotIn("Stop-Process -Name powershell", text)
        self.assertNotIn("Stop-Process -Name node", text)

    def test_auto_merge_requires_exact_green_monolith_provenance(self):
        text = (ROOT / ".github" / "workflows" / "monolith-auto-merge.yml").read_text(encoding="utf-8")
        self.assertIn('workflows: ["LIFE OS CI"]', text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn("monolith/auto/*", text)
        self.assertIn(".head.sha == $sha", text)
        self.assertIn(".base.ref == \"main\"", text)
        self.assertIn(".draft == false", text)
        self.assertIn("Automated verified engineering run", text)
        self.assertIn("-f sha=\"$HEAD_SHA\"", text)

    def test_auto_merge_requires_ci_against_unchanged_main(self):
        text = (ROOT / ".github" / "workflows" / "monolith-auto-merge.yml").read_text(encoding="utf-8")
        self.assertIn("CI_BASE_SHA: ${{ github.event.workflow_run.pull_requests[0].base.sha }}", text)
        self.assertIn("current_main_sha", text)
        self.assertIn("current_pr_base_sha", text)
        self.assertIn('"$current_main_sha" != "$CI_BASE_SHA"', text)
        self.assertIn('"$current_pr_base_sha" != "$CI_BASE_SHA"', text)
        self.assertIn("refresh/retest before merge", text)

    def test_auto_merge_cannot_rewrite_its_own_ci_authority(self):
        text = (ROOT / ".github" / "workflows" / "monolith-auto-merge.yml").read_text(encoding="utf-8")
        self.assertIn("ci\\.yml|monolith-auto-merge\\.yml", text)
        self.assertIn("require an explicit owner merge", text)


if __name__ == "__main__":
    unittest.main()
