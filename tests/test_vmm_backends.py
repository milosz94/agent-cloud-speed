"""acspeed must run a live benchmark on Linux, macOS and Windows.

Firecracker is Linux+KVM only and its maintainers decline the other two hosts, so the portable
backend is QEMU, which takes each OS's own hypervisor as an accelerator: KVM, Hypervisor.framework,
WHPX. Because the guest is then a first-level VM, none of it needs nested virtualization.

What these tests can and cannot do, stated plainly: the QEMU path is exercised for real on Linux
(see test_qemu_boots_the_guest, which boots the actual rootfs). The macOS and Windows paths are
verified only at the level of the command constructed for them, because this suite runs on Linux.
That is the difference between "the argv is right" and "it boots", and only the first is claimed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sandbox"))

import vmjob  # noqa: E402


class VmmSelection(unittest.TestCase):

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"ACSPEED_VMM": "qemu"}):
            self.assertEqual(vmjob.default_vmm(), "qemu")

    def test_non_linux_never_picks_firecracker(self):
        """Firecracker cannot run on macOS or Windows at all, so it must never be selected there."""
        for plat in ("darwin", "win32"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ACSPEED_VMM", None)
                with mock.patch.object(vmjob.sys, "platform", plat):
                    self.assertEqual(vmjob.default_vmm(), "qemu", f"on {plat}")

    def test_linux_without_a_firecracker_binary_falls_back_to_qemu(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACSPEED_VMM", None)
            with mock.patch.object(vmjob.sys, "platform", "linux"), \
                 mock.patch.object(vmjob.os.path, "exists", return_value=False):
                self.assertEqual(vmjob.default_vmm(), "qemu")


class AcceleratorPerOs(unittest.TestCase):
    """The whole portability claim in one mapping."""

    def test_each_os_uses_its_own_hypervisor(self):
        for plat, want in (("linux", "kvm"), ("darwin", "hvf"), ("win32", "whpx")):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ACSPEED_QEMU_ACCEL", None)
                with mock.patch.object(vmjob.sys, "platform", plat):
                    self.assertEqual(vmjob.qemu_accel(), want, f"on {plat}")

    def test_accel_is_overridable(self):
        with mock.patch.dict(os.environ, {"ACSPEED_QEMU_ACCEL": "tcg"}):
            self.assertEqual(vmjob.qemu_accel(), "tcg")

    def test_binary_follows_the_architecture(self):
        for machine, want in (("x86_64", "qemu-system-x86_64"), ("arm64", "qemu-system-aarch64"),
                              ("aarch64", "qemu-system-aarch64")):
            with mock.patch("platform.machine", return_value=machine):
                self.assertEqual(vmjob.qemu_binary(), want, f"on {machine}")


class QemuCommand(unittest.TestCase):
    """What would be run on a Mac or a Windows box, checked here because it cannot be run here."""

    def _argv(self, plat):
        seen = {}

        class _P:
            pid = 1234
            def wait(self, timeout=None): return 0
        def _spawn(cmd, **kw):
            seen["cmd"] = cmd
            return _P()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACSPEED_QEMU_ACCEL", None)
            with mock.patch.object(vmjob.sys, "platform", plat), \
                 mock.patch.object(vmjob.procguard, "spawn", _spawn), \
                 mock.patch.object(vmjob.procguard, "untrack", lambda *a: None), \
                 tempfile.TemporaryDirectory() as d:
                bl = os.path.join(d, "boot.log")
                vmjob._launch_qemu(rootfs=os.path.join(d, "r.ext4"), job=os.path.join(d, "j.ext4"),
                                   vcpus=2, mem_mib=2048, boot_log=bl, timeout=5)
        return seen["cmd"]

    def test_macos_asks_for_hypervisor_framework(self):
        argv = self._argv("darwin")
        self.assertIn("q35,accel=hvf", argv)

    def test_windows_asks_for_whpx(self):
        argv = self._argv("win32")
        self.assertIn("q35,accel=whpx", argv)

    def test_windows_does_not_ask_for_cpu_host(self):
        """WHPX rejects -cpu host; asking for it fails the boot with an opaque error."""
        argv = self._argv("win32")
        self.assertNotIn("host", argv[argv.index("-cpu") + 1])

    def test_linux_uses_cpu_host(self):
        argv = self._argv("linux")
        self.assertEqual(argv[argv.index("-cpu") + 1], "host")

    def test_the_guest_is_told_to_run_vm_runner(self):
        argv = self._argv("linux")
        append = argv[argv.index("-append") + 1]
        self.assertIn("init=/usr/local/bin/vm-runner", append)
        self.assertIn("root=/dev/vda", append)

    def test_both_disks_are_attached_as_virtio_pci(self):
        """The Firecracker CI kernel has no virtio-PCI; the QEMU path uses a distro kernel and PCI."""
        argv = self._argv("linux")
        self.assertEqual(argv.count("virtio-blk-pci,drive=root"), 1)
        self.assertEqual(argv.count("virtio-blk-pci,drive=job"), 1)

    def test_no_reboot_so_the_guest_poweroff_ends_the_process(self):
        self.assertIn("-no-reboot", self._argv("linux"))

    def test_serial_goes_to_the_boot_log_the_poller_tails(self):
        argv = self._argv("linux")
        self.assertTrue(any(str(a).startswith("file:") for a in argv),
                        "the URL relay reads guest console bytes from the boot log file")

    def test_user_mode_networking_needs_no_host_tap(self):
        """The tap pool is Linux-only and needs root; macOS and Windows must need no net setup."""
        argv = self._argv("darwin")
        self.assertIn("user,id=n0", argv)


class SubstrateDisclosure(unittest.TestCase):
    """A QEMU run and a Firecracker run are not silently comparable, so every run records which."""

    def test_record_names_the_vmm_and_its_network_path(self):
        with mock.patch.dict(os.environ, {"ACSPEED_VMM": "qemu"}):
            d = vmjob.describe_substrate()
        self.assertEqual(d["vmm"], "qemu")
        self.assertEqual(d["network"], "user-slirp")
        for key in ("os", "arch", "accel", "vmm_version"):
            self.assertIn(key, d)

    def test_firecracker_and_qemu_describe_differently(self):
        with mock.patch.dict(os.environ, {"ACSPEED_VMM": "qemu"}):
            q = vmjob.describe_substrate()
        with mock.patch.dict(os.environ, {"ACSPEED_VMM": "firecracker"}):
            f = vmjob.describe_substrate()
        self.assertNotEqual((q["vmm"], q["network"]), (f["vmm"], f["network"]),
                            "two substrates must not look identical in the record")


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("qemu-system-x86_64")
                     and os.path.exists(os.path.join(ROOT, "sandbox", "images", "vmlinuz"))
                     and os.path.exists(os.path.join(ROOT, "sandbox", "images", "rootfs.ext4")),
                     "needs Linux, qemu, and a built QEMU kernel + rootfs")
class QemuReallyBoots(unittest.TestCase):
    """Not a mock: boot the real guest and require the real runner to report in."""

    def test_qemu_boots_the_guest_and_vm_runner_runs(self):
        with tempfile.TemporaryDirectory() as d:
            overlay = os.path.join(d, "root.qcow2")
            subprocess.run(["qemu-img", "create", "-f", "qcow2", "-F", "raw",
                            "-b", os.path.join(ROOT, "sandbox", "images", "rootfs.ext4"), overlay],
                           check=True, capture_output=True)
            boot_log = os.path.join(d, "boot.log")
            cmd = [vmjob.qemu_binary(), "-machine", "q35,accel=kvm", "-cpu", "host",
                   "-m", "2048", "-smp", "2",
                   "-kernel", vmjob.QEMU_KERNEL, "-initrd", vmjob.QEMU_INITRD,
                   "-append", "console=ttyS0 root=/dev/vda rw init=/usr/local/bin/vm-runner "
                              "panic=1 reboot=t",
                   "-drive", f"id=root,file={overlay},format=qcow2,if=none",
                   "-device", "virtio-blk-pci,drive=root",
                   "-serial", "file:" + boot_log,
                   "-no-reboot", "-nographic", "-display", "none"]
            subprocess.run(cmd, timeout=180, capture_output=True, stdin=subprocess.DEVNULL)
            log = open(boot_log, errors="replace").read()
        self.assertIn("[vm-runner]", log, "the guest never reached the acspeed runner")
        self.assertIn("SMOKE OK", log, "the runner started but did not complete its check")


class ExperimentalPlatforms(unittest.TestCase):
    """macOS and Windows are unverified, and that must be impossible to miss or to lose."""

    def test_linux_is_not_experimental(self):
        with mock.patch.object(vmjob.sys, "platform", "linux"):
            self.assertFalse(vmjob.platform_is_experimental())
            self.assertEqual(vmjob.experimental_notice(), "")

    def test_mac_and_windows_are_experimental(self):
        for plat, name in (("darwin", "macOS"), ("win32", "Windows")):
            with mock.patch.object(vmjob.sys, "platform", plat):
                self.assertTrue(vmjob.platform_is_experimental(), plat)
                self.assertIn(name, vmjob.experimental_notice())

    def test_the_record_carries_the_flag(self):
        """Printed warnings are lost; the record is what someone reads months later."""
        for plat, want in (("linux", False), ("darwin", True), ("win32", True)):
            with mock.patch.object(vmjob.sys, "platform", plat):
                self.assertEqual(vmjob.describe_substrate()["experimental"], want, plat)


class ExperimentalRunsAreNotPooled(unittest.TestCase):

    def test_build_tables_skips_an_experimental_record(self):
        import json as _json
        sys.path.insert(0, ROOT)
        import build_tables
        with tempfile.TemporaryDirectory() as d:
            good = {"first_attempt_success": True, "split": {"a": 1},
                    "substrate": {"os": "linux", "experimental": False}}
            bad = {"first_attempt_success": True, "split": {"a": 1},
                   "substrate": {"os": "darwin", "experimental": True}}
            for name, rec in (("run01.json", good), ("run02.json", bad)):
                with open(os.path.join(d, name), "w") as fh:
                    _json.dump(rec, fh)
            rows = build_tables.load_runs(d)
        self.assertEqual(len(rows), 1, "an experimental run must not be averaged in with verified ones")
        self.assertFalse(rows[0]["substrate"]["experimental"])


if __name__ == "__main__":
    unittest.main()
