#!/usr/bin/python3
#
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

import os
import shutil
import subprocess
import tempfile
import unittest


class SyncDirTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)
        self.make_ubuntu_core_rootfs_script_testable()
        self.make_ubuntu_core_rootfs_runner()

    def make_ubuntu_core_rootfs_script_testable(self):
        """Copy ubuntu-core-rootfs into tmpdir and fixup imports"""
        basedir = os.path.join(os.path.dirname(__file__), "..")
        dst = os.path.join(self.tmpdir, "ubuntu-core-rootfs")
        shutil.copy(os.path.join(basedir, "scripts/ubuntu-core-rootfs"), dst)
        with open(dst) as fin:
            with open(dst+".tmp", "w") as fout:
                for line in fin:
                    line = line.replace("/scripts/ubuntu-core-functions", basedir+"/scripts/ubuntu-core-functions")
                    fout.write(line)
        os.rename(dst+".tmp", dst)

    def make_ubuntu_core_rootfs_runner(self):
        with open(os.path.join(self.tmpdir, "runner.sh"), "w") as fp:
            fp.write("""#!/bin/sh
. ./ubuntu-core-rootfs

sync_dirs "$1" "$2" "$3"
""")

    def make_mock_sync_env(self):
        """
        Create a mock environment for sync_dir() that is close to thing.
        """
        base = self.tmpdir
        source =  "etc"
        os.makedirs(os.path.join(base, source))
        target = "writable/"
        os.makedirs(os.path.join(base, target, source))
        return base, source, target

    def run_sync_dir(self, base, source, target):
        """
        Run the sync_dir script in isolation with the given inputs for
        base, source, target.
        """
        subprocess.check_call(["sh", "-e", "runner.sh", base, source, target], cwd=self.tmpdir)

    def test_sync_dir_trivial(self):
        """Ensure trivial sync works"""
        base, source, target = self.make_mock_sync_env()
        with open(os.path.join(base, source, "canary.txt"), "w"):
            pass

        self.run_sync_dir(base, source, target)
        self.assertTrue(os.path.exists(os.path.join(base, target, "etc/canary.txt")))

    def test_sync_dir_broken_symlink(self):
        """Ensure sync with a (broken) symlink works"""
        base, source, target = self.make_mock_sync_env()
        os.symlink("invalid-symlink", os.path.join(base, source, "broken-symlink"))

        self.run_sync_dir(base, source, target)
        self.assertTrue(os.path.lexists(os.path.join(base, target, "etc/broken-symlink")))
        
