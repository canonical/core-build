#!/usr/bin/env python3
# Copyright (C) 2017 Canonical Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Unit tests for initrd shell scripts."""

from helpers import VMShellTestCase, main


class UbuntuCoreFunctionsTests(VMShellTestCase):
    """Tests for shell code in ubuntu-core-functions."""

    MOCK = ("log_begin_msg", "log_end_msg", "run_scripts", "wait-for-root")

    def setUp(self) -> None:
        """
        Prepare each test for exaction.

        This mocks some shell functions and executables (those listed in MOCK)
        as well as the panic command. It also sources the "ubuntu-core-rootfs"
        script so that it can be easily tested.
        """
        super().setUp()
        # Test certain functions are mocked.
        for fn in self.MOCK:
            self.sh_mock(fn)
        # Mock "panic" to exit unsuccessfully.
        self.sh_mock("panic", exits=150)
        # Source ubuntu-core-rootfs
        self.sh_source("/scripts/ubuntu-core-rootfs")

    def test_pre_mountroot__works(self) -> None:
        """Test pre_mountroot runs local-top scripts."""
        returncode, log = self.sh_run("pre_mountroot")
        self.assertEqual(returncode, 0)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            ("log_begin_msg", "Running /scripts/local-top"),
            ("run_scripts", "/scripts/local-top"),
            ("log_end_msg",),
        ])

    def test_pre_mountroot__respects_quiet(self) -> None:
        """Test pre_mountroot doesn't log with quiet=y."""
        self.sh_inject("quiet=y")
        returncode, log = self.sh_run("pre_mountroot")
        # XXX: error code left-over from [ ] used inside pre_mountroot()
        self.assertEqual(returncode, 1)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            ("run_scripts", "/scripts/local-top"),
        ])

    def test_get_partition_from_label__works(self) -> None:
        """Test get_partition_from_label when working normally."""
        # NOTE: the device has to actually exist as the code uses "readlink -f"
        # to canonicalize it.
        self.sh_inject("ln -s /dev/null /dev/some-label")
        returncode, log = self.sh_run("get_partition_from_label some-label")
        self.assertEqual(returncode, 0)
        self.assertEqual(log, [b"/dev/null"])
        self.assertEqual(self.sh_mocked_calls(), [
            ("wait-for-root", "LABEL=some-label", "180"),
        ])

    def test_get_partition_from_label__respects_ROOTDELAY(self) -> None:
        """Test get_partition_from_label respects ROOTDELAY variable."""
        self.sh_inject("ln -s /dev/null /dev/some-label")
        self.sh_inject("ROOTDELAY=123")
        returncode, log = self.sh_run("get_partition_from_label some-label")
        self.assertEqual(returncode, 0)
        self.assertEqual(log, [b"/dev/null"])
        self.assertEqual(self.sh_mocked_calls(), [
            ("wait-for-root", "LABEL=some-label", "123"),
        ])

    def test_get_partition_from_label__failing_wait_for_root(self) -> None:
        """Test get_partition_from_label respects ROOTDELAY variable."""
        self.sh_inject("ln -s /dev/null /dev/some-label")
        self.sh_inject("ROOTDELAY=123")
        self.sh_mock("wait-for-root", returns=10)
        returncode, log = self.sh_run("get_partition_from_label some-label")
        self.assertEqual(returncode, 0)
        self.assertEqual(log, [b"/dev/null"])
        self.assertEqual(self.sh_mocked_calls(), [
            ("wait-for-root", "LABEL=some-label", "123"),
        ])

    def test_get_partition_from_label__without_label(self) -> None:
        """Test get_partition_from_label when invoked without any label."""
        returncode, log = self.sh_run("get_partition_from_label")
        self.assertEqual(returncode, 150)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            ("panic", "need FS label"),
        ])

    def test_get_partition_from_label__unknown_label(self) -> None:
        """Test get_partition_from_label when the label is not found."""
        returncode, log = self.sh_run("get_partition_from_label some-label")
        self.assertEqual(returncode, 0)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            ("wait-for-root", "LABEL=some-label", "180"),
        ])

    def test_get_partition_from_label__broken_label(self) -> None:
        """Test get_partition_from_label when the label is a broken symlink."""
        self.sh_inject("ln -s /dev/BOGUS /dev/some-label")
        returncode, log = self.sh_run("get_partition_from_label some-label")
        self.assertEqual(returncode, 1)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            ("wait-for-root", "LABEL=some-label", "180"),
        ])

    def test_do_root_mounting__with_unset_writable_label(self) -> None:
        """Test do_root_mounting panics when writable_label is unset."""
        returncode, log = self.sh_run("do_root_mounting")
        self.assertEqual(returncode, 150)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            # FIXME: re-enable after solving udev issues in spike
            #("wait-for-root", "LABEL=", "180"),
            ("panic", "root device  does not exist"),
        ])

    def test_do_root_mounting__with_failing_wait_for_root(self) -> None:
        """Test do_root_mounting panics when writable_label is unset."""
        self.sh_mock("wait-for-root", returns=10)
        self.sh_inject("writable_label=some-label")
        returncode, log = self.sh_run("do_root_mounting")
        self.assertEqual(returncode, 150)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            # FIXME: re-enable after solving udev issues in spike
            #("wait-for-root", "LABEL=some-label", "180"),
            #("panic", "unable to find root partition LABEL=some-label"),
            ("panic", "root device  does not exist"),
        ])

    def test_do_root_mounting__works(self) -> None:
        """Test do_root_mounting works when writable_label is set correctly."""
        self.sh_inject("writable_label=some-label")
        self.sh_inject("writable_mnt=/fake-writable-mnt")
        self.sh_inject("ln -s /dev/null /dev/some-label")
        self.sh_mock("findfs", prints="/dev/zero")
        self.sh_mock("mount")
        self.sh_mock("modprobe")
        returncode, log = self.sh_run("do_root_mounting")
        self.assertEqual(returncode, 0)
        self.assertEqual(log, [])
        self.assertEqual(self.sh_mocked_calls(), [
            # FIXME: re-enable after solving udev issues in spike
            #("wait-for-root", "LABEL=some-label", "180"),
            ("findfs", "LABEL=some-label"),
            ("modprobe", "squashfs"),
            ("wait-for-root", "LABEL=some-label", "180"),
            ("mount", "/dev/null", "/fake-writable-mnt"),
        ])


if __name__ == "__main__":
    # logging.basicConfig(level=logging.DEBUG)
    main()
