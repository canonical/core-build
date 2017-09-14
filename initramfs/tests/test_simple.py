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

    def test_sync_dir_trivial(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)

        # ensure we can test it
        basedir = os.path.join(os.path.dirname(__file__), "..")
        dst = os.path.join(tmpdir, "ubuntu-core-rootfs")
        shutil.copy(os.path.join(basedir, "scripts/ubuntu-core-rootfs"), dst)
        with open(dst) as fin:
            with open(dst+".tmp", "w") as fout:
                for line in fin:
                    line = line.replace("/scripts/ubuntu-core-functions", basedir+"/scripts/ubuntu-core-functions")
                    fout.write(line)
        os.rename(dst+".tmp", dst)

        # write shell wrapper for sync_dirs()
        with open(os.path.join(tmpdir, "runner.sh"), "w") as fp:
            fp.write("""#!/bin/sh
. ./ubuntu-core-rootfs

sync_dirs "$1" "$2" "$3"
""")
        
        # actually test it
        base = tmpdir
        source =  "etc"
        os.makedirs(os.path.join(base, source))
        target = "writable/"
        os.makedirs(os.path.join(base, target, source))

        with open(os.path.join(base, source, "canary.txt"), "w"):
            pass
        subprocess.check_call(["sh", "-e", "runner.sh", base, source, target], cwd=tmpdir)
        
        self.assertTrue(os.path.exists(os.path.join(base, target, "etc/canary.txt")))
