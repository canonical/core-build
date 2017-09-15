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

"""Support code for testing initrd scripts in qemu."""

import asyncio
import datetime
import errno
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import types
import unittest

from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    cast,
)

__all__ = ('VMShellTestCase')

_logger = logging.getLogger("qemu")


O_CLOEXEC = 0x80000


class Resource:
    """Host resource that needs cleanup after use."""

    def cleanup(self) -> None:
        """Release the host resources associated with this object."""

    def __del__(self) -> None:
        self.cleanup()


class FIFO(Resource):
    """Named pipe used for communication with qemu."""

    @classmethod
    def create(cls: Type, path: str, mode: str) -> "FIFO":
        if mode != 'r' and mode != 'w':
            raise ValueError("cannot create FIFO with mode {!a}".format(mode))
        try:
            os.mkfifo(path)
        except FileExistsError:
            pass
        return FIFO(path, mode)

    def __init__(self, path: str, mode: str) -> None:
        self._path = path  # type: Optional[str]
        self._mode = mode
        self._reader = None  # type: Optional[asyncio.StreamReader]
        self._writer = None  # type: Optional[asyncio.StreamWriter]
        self._transport = None  # type: Optional[asyncio.BaseTransport]
        self._protocol = None  # type: Optional[asyncio.BaseProtocol]

    def __del__(self) -> None:
        self.cleanup()

    @property
    def path(self) -> Optional[str]:
        """Get the path of the named pipe."""
        return self._path

    @property
    def mode(self) -> str:
        """Get the mode as applicable to open()."""
        return self._mode

    @property
    def reader(self) -> Optional[asyncio.StreamReader]:
        if self.mode != 'r':
            raise AttributeError(
                    "reader is only available on FIFOs open with mode='r'")
        return self._reader

    @property
    def writer(self) -> Optional[asyncio.StreamWriter]:
        if self.mode != 'w':
            raise AttributeError(
                    "writer is only available on FIFOs open with mode='w'")
        return self._writer

    async def open(self) -> None:
        if self._transport is not None or self._protocol is not None:
            return
        loop = asyncio.get_event_loop()
        if self.mode == 'r':
            r_t_p = await self._open_reader(loop)
            self._reader, self._transport, self._protocol = r_t_p
        elif self.mode == 'w':
            w_t_p = await self._open_writer(loop)
            self._writer, self._transport, self._protocol = w_t_p

    async def _open_reader(self, loop: asyncio.AbstractEventLoop) \
            -> Tuple[asyncio.StreamReader, asyncio.ReadTransport,
                     asyncio.StreamReaderProtocol]:
        if self.path is None:
            raise ValueError("cannot open fifo when path is not set")
        fd = os.open(self.path, os.O_NONBLOCK | O_CLOEXEC | os.O_RDONLY)
        reader = asyncio.StreamReader(loop=loop)

        def proto_factory() -> asyncio.StreamReaderProtocol:
            return asyncio.StreamReaderProtocol(reader, loop=loop)
        transport, protocol = await loop.connect_read_pipe(
                proto_factory, open(fd, "rb", buffering=0))
        return (reader, cast(asyncio.ReadTransport, transport),
                cast(asyncio.StreamReaderProtocol, protocol))

    async def _open_writer(self, loop: asyncio.AbstractEventLoop) \
            -> Tuple[asyncio.StreamWriter, asyncio.WriteTransport,
                     asyncio.streams.FlowControlMixin]:
        if self.path is None:
            raise ValueError("cannot open fifo when path is not set")
        fd = None
        while fd is None:
            try:
                fd = os.open(
                    self.path, os.O_WRONLY | os.O_NONBLOCK | O_CLOEXEC)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    asyncio.sleep(1)

        def proto_factory() -> asyncio.streams.FlowControlMixin:
            return asyncio.streams.FlowControlMixin()
        transport, protocol = await loop.connect_write_pipe(
                proto_factory, os.fdopen(fd, "wb", buffering=0))
        writer = asyncio.StreamWriter(transport, protocol, None, loop)
        return writer, transport, protocol

    def cleanup(self) -> None:
        """Close and remove the named pipe from the filesystem."""
        if self._transport is not None:
            self._transport.close()
        if self.path is not None:
            try:
                os.unlink(self.path)
                self._path = None
            except FileNotFoundError:
                pass


class CharDev:
    """QEMU character device."""

    def __init__(self, qemu_id: str, qemu_cmd: str, attrs: Dict[str, Any]) \
            -> None:
        """Initialize a QEMU character device."""
        self.qemu_id = qemu_id
        self.qemu_cmd = qemu_cmd
        self.attrs = attrs
        self.resources = []  # type: List[Resource]

    def add_resource(self, resource: Resource) -> None:
        """Add a resource associated with this character device."""
        self.resources.append(resource)

    @property
    def qemu_options(self) -> Tuple[str, ...]:
        """Get the additional options to qemu executable."""
        return ('-chardev', self.qemu_cmd)


class Device:
    """QEMU device."""

    def __init__(self, qemu_type: str, qemu_cmd: str, attrs: Dict[str, Any]) \
            -> None:
        """Initialize a QEMU device with given type nd command line option."""
        self.qemu_type = qemu_type
        self.qemu_cmd = qemu_cmd
        self.attrs = attrs

    @property
    def qemu_options(self) -> Tuple[str, ...]:
        """Get the additional options to qemu executable."""
        return ('-device', '{},{}'.format(self.qemu_type, self.qemu_cmd))


class Drive:
    """QEMU drive."""

    def __init__(self, options: Dict[str, str]) -> None:
        """Initialize a QEMU drive with given key=value options."""
        self.options = options

    @property
    def qemu_options(self) -> Tuple[str, ...]:
        """Get the additional options to qemu executable."""
        return ('-drive', ','.join('{}={}'.format(opt, self.options[opt])
                for opt in sorted(self.options)))


class SerialPortFIFOs:
    """QEMU serial port associated with two FIFOs."""

    def __init__(self, chardev: CharDev, device: Device,
                 attrs: Dict[str, Any]) -> None:
        self._chardev = chardev
        self._device = device
        self._attrs = attrs

    @property
    def device(self) -> Device:
        """Get the QEMU device associated with the serial port."""
        return self._device

    @property
    def guest_ttyname(self) -> str:
        """Get the name of the tty as seen by the guest (e.g. ttyS0)."""
        return cast(str, self._device.attrs["guest-ttyname"])

    @property
    def fifo_in(self) -> FIFO:
        """Get the FIFO for writing to the serial port."""
        return cast(FIFO, self._attrs['fifo-in'])

    @property
    def fifo_out(self) -> FIFO:
        """Get the FIFO for reading from the serial port."""
        return cast(FIFO, self._attrs['fifo-out'])

    @property
    def reader(self) -> Optional[asyncio.StreamReader]:
        """Get the stream reader for reading from this serial port."""
        return self.fifo_out.reader

    @property
    def writer(self) -> Optional[asyncio.StreamWriter]:
        """Get the stream writer for writing to this serial port."""
        return self.fifo_in.writer


class MonitorFIFOs:
    """QEMU monitor associated with two FIFOs."""

    def __init__(self, chardev: CharDev, attrs: Dict[str, Any]) -> None:
        self._chardev = chardev
        self._attrs = attrs

    @property
    def fifo_in(self) -> FIFO:
        """Get the FIFO for writing to the monitor."""
        return cast(FIFO, self._attrs['fifo-in'])

    @property
    def fifo_out(self) -> FIFO:
        """Get the FIFO for reading from the monitor."""
        return cast(FIFO, self._attrs['fifo-out'])

    @property
    def reader(self) -> Optional[asyncio.StreamReader]:
        """Get the stream reader for reading from the monitor."""
        return self.fifo_out.reader

    @property
    def writer(self) -> Optional[asyncio.StreamWriter]:
        """Get the stream writer for writing to the monitor."""
        return self.fifo_in.writer


class Qemu:
    """High-level wrapper around qemu system emulator."""

    def __init__(self, exe: str) -> None:
        self._exe = exe
        self._enable_kvm = False
        self._snapshot = False
        # Memory size in megabytes.
        self._memory = None  # type: Optional[int]
        # Kernel image, initrd image and kernel command line
        self._kernel = None  # type: Optional[str]
        self._initrd = None  # type: Optional[str]
        # append arguments to kernel command line
        self._append = None  # type: Optional[str]
        # Type of display to use
        self._display = None  # type: Optional[str]
        # Type of QEMU monitor to use
        self._monitor = None  # type: Optional[str]
        # Additional character devices
        self._chardevs = {}  # type: Dict[str, CharDev]
        # Additional devices and drives
        self._devices = []  # type: List[Device]
        self._drives = []  # type: List[Drive]

    @property
    def enable_kvm(self) -> bool:
        """Get the flag controlling kernel virtual machine (KVM)."""
        return self._enable_kvm

    @enable_kvm.setter
    def enable_kvm(self, value: bool) -> None:
        self._enable_kvm = bool(value)

    @property
    def snapshot(self) -> bool:
        """Get the flag controlling global snapshot mode."""
        return self._snapshot

    @snapshot.setter
    def snapshot(self, value: bool) -> None:
        self._snapshot = bool(value)

    @property
    def memory(self) -> Optional[int]:
        """Get the size of RAM in megabytes."""
        return self._memory

    @memory.setter
    def memory(self, value: int) -> None:
        if 0 >= value > 4096:
            raise ValueError("cannot set memory size to {!a}".format(value))
        self._memory = value

    @property
    def kernel(self) -> Optional[str]:
        """Get the kernel image to use."""
        return self._kernel

    @kernel.setter
    def kernel(self, value: str) -> None:
        self._kernel = value

    @property
    def initrd(self) -> Optional[str]:
        """Get the initial ramdisk or ramfs image to use."""
        return self._initrd

    @initrd.setter
    def initrd(self, value: str) -> None:
        self._initrd = value

    @property
    def append(self) -> Optional[str]:
        """Get arguments appended to the kernel command line."""
        return self._append

    @append.setter
    def append(self, value: str) -> None:
        self._append = value

    @property
    def display(self) -> Optional[str]:
        """Get the type of display to use (sdl, curses, none, gtk, vnc)."""
        return self._display

    @display.setter
    def display(self, value: str) -> None:
        if value not in ('sdl', 'curses', 'none', 'gtk', 'vnc', None):
            raise ValueError("cannot set display type {!a}".format(value))
        self._display = value

    @property
    def monitor(self) -> Optional[str]:
        """Get the location of the QEMU machine monitor."""
        return self._monitor

    @monitor.setter
    def monitor(self, value: str) -> None:
        if (value not in ('vc', 'stdio', 'none', None) and
                not value.startswith("chardev:")):
            raise ValueError("cannot set monitor location {!a}".format(value))
        self._monitor = value

    def __enter__(self) -> "Qemu":
        return self

    def __exit__(self, exc_type: Type, exc_value: Exception,
                 traceback: types.TracebackType) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        for chardev in self._chardevs.values():
            for resource in chardev.resources:
                resource.cleanup()

    async def start(self, *extra_args: str) -> asyncio.subprocess.Process:
        """Start QEMU and open all named pipes."""
        args = self._qemu_cmdline(extra_args)
        _logger.info("starting: %r", args)
        # Run qemu in a separate process
        proc = await asyncio.create_subprocess_exec(*args)
        # Open all the FIFOs associated with any character devices we may have.
        for qemu_id in sorted(self._chardevs):
            chardev = self._chardevs[qemu_id]
            for resource in chardev.resources:
                if isinstance(resource, FIFO):
                    await resource.open()
        return proc

    def _qemu_cmdline(self, extra_args: Sequence[str]) -> List[str]:
        args = [self._exe]
        # Enable KVM if requested.
        if self.enable_kvm:
            args.append("-enable-kvm")
        # Enable global snapshot mode if requested.
        if self.snapshot:
            args.append("-snapshot")
        # Set default memory size unless overriden.
        if self.memory is not None and "-m" not in extra_args:
            args.append("-m")
            args.append(str(self.memory))
        # Set kernel / initrd / command line, if available
        if self.kernel is not None:
            args.append("-kernel")
            args.append(self.kernel)
        if self.initrd is not None:
            args.append("-initrd")
            args.append(self.initrd)
        if self.append is not None:
            args.append("-append")
            args.append(self.append)
        # Add command line arguments for all character devices.
        for qemu_id in sorted(self._chardevs):
            chardev = self._chardevs[qemu_id]
            args.extend(chardev.qemu_options)
        # Set display type if desired
        if self.display is not None:
            args.append("-display")
            args.append(self.display)
        # Set QEMU monitor type if desired
        if self.monitor is not None:
            args.append("-monitor")
            args.append(self.monitor)
        # Add command line arguments for all devices.
        for device in self._devices:
            args.extend(device.qemu_options)
        for drive in self._drives:
            args.extend(drive.qemu_options)
        # Add any additional arguments
        args.extend(extra_args)
        return args

    def add_chardev_pipe(self, qemu_id: str, path: str) -> CharDev:
        """
        Add a character device associated with a pipe.

        :arg qemu_id:
            Internal qemu identifier, can be associated with qemu
            devices later (such as a isa-serial-port device).
        :arg path:
            Base name of the two named pipes to create.
        :returns:
            CharDev with two FIFOs (in, out) as resources.

        The character device will have an internal qemu identifier of
        `qemu_id`, which must be unique in a given invocation of qemu.

        Two named pipes called `path`.in and `path`.out are automatically
        created. The one ending with .in is meant for writing, the one ending
        with .out is meant for reading. Those are managed internally and will
        be removed along with the qemu object.
        """
        if qemu_id in self._chardevs:
            raise ValueError(
                "cannot use identifier {!a}, already used".format(qemu_id))
        fifo_in = FIFO.create(path + ".in", "w")
        fifo_out = FIFO.create(path + ".out", "r")
        qemu_cmd = 'pipe,id={},path={}'.format(qemu_id, path)
        chardev = CharDev(qemu_id, qemu_cmd, {
            'fifo-in': fifo_in,
            'fifo-out': fifo_out,
        })
        chardev.add_resource(fifo_in)
        chardev.add_resource(fifo_out)
        self._chardevs[qemu_id] = chardev
        return chardev

    def remove_chardev(self, chardev: CharDev) -> None:
        """
        Remove a character device.

        Character devices can only be removed before the virtual
        machine is started.
        """
        del self._chardevs[chardev.qemu_id]
        for resource in chardev.resources:
            resource.cleanup()

    def add_device_isa_serial(self, qemu_chardev_id: str) -> Device:
        """
        Add a serial port to the virtual machine.

        :arg qemu_chardev_id:
            Internal qemu identifier that must refer to a character device.

        A serial port is associated with a QEMU character device that must
        be added separately earlier. Only four serial ports may be added
        to a single machine.
        """
        if qemu_chardev_id not in self._chardevs:
            raise ValueError(
                "cannot find chardev {!a}".format(qemu_chardev_id))
        count = 0
        for device in self._devices:
            if device.qemu_type == "isa-serial":
                count += 1
        if count >= 4:
            raise ValueError("cannot add more than four isa-serial devices")
        device = Device("isa-serial", "chardev={}".format(qemu_chardev_id), {
            "guest-ttyname": "ttyS{}".format(count)
        })
        self._devices.append(device)
        return device

    def add_device_isa_debug_exit(self) -> Device:
        """
        Add a debugging device that can instruct QEMU to exit.

        This device is available in the IO space at address 0xf4.
        When written to the virtual machine with exit with the return code
        `(1 | (1 << code))`.
        """
        for device in self._devices:
            if device.qemu_type == "isa-debug-exit":
                raise ValueError("cannot add another isa-debug-exit device")
        device = Device("isa-debug-exit", "iobase=0xf4,iosize=0x4", {
            "qemu-iobase": 0xf4,
            "qemu-iosize": 0x04,
        })
        self._devices.append(device)
        return device

    def add_serial_port_with_fifos(self, qemu_id: str) -> SerialPortFIFOs:
        """
        Add a QEMU pipe chardev and associate it with a ISA serial port.

        This accomplishes a common task of getting a usable serial
        port easily. After starting the virtual machine the caller
        can refer to the `fifo_in` and `fifo_out` properties to interact
        with the serial port. The associated resources are automatically
        managed and are cleaned up when the machine terminates.
        """
        file_name = tempfile.mktemp(prefix=qemu_id)
        chardev = self.add_chardev_pipe(qemu_id, file_name)
        try:
            device = self.add_device_isa_serial(qemu_id)
        except ValueError:
            self.remove_chardev(chardev)
        return SerialPortFIFOs(chardev, device, {
            "fifo-in": chardev.attrs["fifo-in"],
            "fifo-out": chardev.attrs["fifo-out"],
        })

    def add_monitor_with_fifos(self, qemu_id: str='monitor') -> MonitorFIFOs:
        """Add a QEMU pipe chardev and associate it with the QEMU monitor."""
        file_name = tempfile.mktemp(prefix=qemu_id)
        chardev = self.add_chardev_pipe(qemu_id, file_name)
        self.monitor = "chardev:{}".format(chardev.qemu_id)
        return MonitorFIFOs(chardev, {
            "fifo-in": chardev.attrs["fifo-in"],
            "fifo-out": chardev.attrs["fifo-out"],
        })

    def add_drive(self, **opts: str) -> Drive:
        """Add a hard disk drive."""
        drive = Drive(opts)
        self._drives.append(drive)
        return drive


class BootError(Exception):
    """Exception raised when we cannot boot successfully."""


class StateError(Exception):
    """Cannot perform operation in the current state."""


class BadRequest(Exception):
    """The requested operation cannot be processed."""


class TestVM:
    """Virtual machine for testing initrd."""

    def __init__(self) -> None:
        # Event set when the machine finished booting.
        # We know this because the init system in the initrd talks to us over
        # the testio serial port. Once this event is reached we can reliably
        # talk to the init system and issue commands.
        self._booted = asyncio.Event()
        # The asyncio.subprocess.Process representing qemu.
        self._proc = None  # type: Optional[asyncio.subprocess.Process]
        self._testio = None  # type: Optional[SerialPortFIFOs]
        self._console = None  # type: Optional[SerialPortFIFOs]
        self._qemu = None  # type: Optional[Qemu]

    def cleanup(self) -> None:
        if self._qemu is not None:
            self._qemu.cleanup()

    async def make_boot_assets(self) -> int:
        """Get the build all assests needed for the test to run."""
        make = await asyncio.create_subprocess_exec("make", "--silent")
        return await make.wait()

    async def boot(self, timeout: int=5) -> None:
        """Wait until the machine boots and is ready for testing."""
        # Use full system emulation of x86_64, with kvm and just enough memory
        # to load our kernel and initrd.
        qemu = self._qemu = Qemu("qemu-system-x86_64")
        if os.path.exists("/dev/kvm"):
            qemu.enable_kvm = True
        qemu.memory = 64

        # Disable display (to run headless)
        qemu.display = "none"

        # Add a small disk and enable global snapshot mode.
        qemu.snapshot = True
        qemu.add_drive(file='disk.img')

        # Redirect QEMU monitor (the human one, not qpm) to another chardev.
        self._monitor = qemu.add_monitor_with_fifos()

        # Add two serial ports backed by local FIFOs:
        #  - console for observing the boot process and simple interactions
        #  - testio for capturing output from tests, reliably
        console = self._console = qemu.add_serial_port_with_fifos("console")
        testio = self._testio = qemu.add_serial_port_with_fifos("testio")

        # Add a special debug device that we can use to exit qemu quickly.
        qemu.add_device_isa_debug_exit()

        # Use our kernel and back-to-back initrd and set command line.
        qemu.kernel = "kernel"
        qemu.initrd = "initrd.back-to-back.cpio.gz"
        qemu.append = " ".join([
            # Boot in quiet mode, this is just nicer.
            "quiet",
            # Redirect console to the "consle" serial port.
            "console={}".format(console.guest_ttyname),
            "--",
            # Instruct our special init process about testio serial port.
            "testio={}".format(testio.guest_ttyname)
        ])
        # Start qemu and process everything.
        # This should finish in a few seconds.
        self._proc = await qemu.start()
        tasks = [
            asyncio.ensure_future(self._booted.wait()),
            asyncio.ensure_future(self._drain_console()),
            asyncio.ensure_future(self._drain_monitor()),
            asyncio.ensure_future(self._drain_testio()),
        ]  # type: List[asyncio.Future[Any]]
        done, pending = await asyncio.wait(
            tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        # Cancel pending tasks and check if we managed to boot.
        for task in pending:
            task.cancel()
        if not self._booted.is_set():
            raise BootError("test init process did not signal boot-ok")

    async def savevm(self, name: str) -> None:
        """Save snapshot of the virtual machine."""
        await self.monitor("savevm {}".format(name))

    async def loadvm(self, name: str) -> None:
        """Load snapshot of the virtual machine."""
        await self.monitor("loadvm {}".format(name))

    async def monitor(self, cmd: str) -> None:
        """
        Issue a request to the QEMU monitor.

        :arg cmd:
            Command for the QEMU monitor.
        """
        reader = self._monitor.reader
        if reader is None:
            raise TypeError("monitor is not ready for reading")
        writer = self._monitor.writer
        if writer is None:
            raise TypeError("monitor is not ready for writing")

        # Write request header and data.
        req = '{}\n'.format(cmd).encode("utf-8")
        _logger.info("(monitor) -> %r", req)
        writer.write(req)

        # Send request data and process console.
        request_task = asyncio.ensure_future(writer.drain())
        console_task = asyncio.ensure_future(self._drain_console())
        try:
            while not request_task.done():
                tasks = [console_task]
                if not request_task.done():
                    tasks.append(request_task)
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not request_task.done():
                request_task.cancel()
            if not console_task.done():
                console_task.cancel()

        # Read qemu response, this will contain the request as it was "typed",
        # plus some ANSI escape codes. Just strip those out and ensure the
        # response is what we expected. This is a poor man's way of handing the
        # protocol intended for humans but it is good enough for this one thing
        # we need.
        resp = await reader.readline()
        resp = resp[resp.rindex(b"\x1b[D") + 3:]
        resp = resp[:resp.index(b"\x1b[K")]
        resp += b"\n"
        if resp != req:
            raise BadRequest(resp)

    async def shutdown(self) -> int:
        """Stop the virtual machine and return the exit code."""
        if self._proc is None:
            raise StateError(
                "cannot stop virtual machine that was not started")
        # If this doesn't complete then just kill qemu.
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        await self._proc.wait()
        returncode = self._proc.returncode
        self._proc = None
        if self._qemu is not None:
            self._qemu.cleanup()
            self._qemu = None
        return returncode

    async def rpc(self, cmd: str, data: bytes=b'', *,
                  timeout: Optional[int]=None, log_output: bool=False) \
            -> Tuple[Optional[Dict[Any, Any]], List[bytes]]:
        """
        Make an RPC request to the init process in the test VM.

        :arg cmd:
            The command to remote init process.
        :arg data:
            Arbitrary data that goes with the command.
        :arg timeout:
            Timeout after which the request fails.
        :arg log_output:
            Flag indicating if console output should be collected.
        :returns:
            Tuple (response, console_log)
        """
        console_log = []  # type: List[bytes]
        if self._testio is None:
            raise TypeError("testio is not ready")
        writer = self._testio.writer
        if writer is None:
            raise TypeError("testio is not ready for writing")

        # Write request header and data.
        req = '{}\n'.format(cmd).encode("utf-8")
        _logger.info("(test io) -> %r", req)
        writer.write(req)
        if len(data) > 0:
            _logger.info("(test io) -> data (%d bytes)", len(data))
            _logger.debug("(test io) << __DATA__")
            for line in data.splitlines():
                _logger.debug('(test io) .. %s', line)
            _logger.debug("(test io) __DATA__")
            writer.write(data)

        # Process all I/O
        request_task = asyncio.ensure_future(writer.drain())
        response_task = asyncio.ensure_future(self._read_and_decode_testio())
        console_task = asyncio.ensure_future(
            self._drain_console(console_log if log_output else None))
        monitor_task = asyncio.ensure_future(self._drain_monitor())
        try:
            while not request_task.done() or not response_task.done():
                tasks = []  # type: List[asyncio.Future[Any]]
                tasks.append(console_task)
                tasks.append(monitor_task)
                if not request_task.done():
                    tasks.append(request_task)
                if not response_task.done():
                    tasks.append(response_task)
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not request_task.done():
                request_task.cancel()
            if not response_task.done():
                response_task.cancel()
            if not console_task.done():
                console_task.cancel()
            if not monitor_task.done():
                monitor_task.cancel()

        # Read response and ensure that it is OK.
        response = response_task.result()
        if response is None or response.get("result") != "ok":
            raise BadRequest(response)

        return (response, console_log)

    async def exit(self) -> None:
        """Issue the exit command."""
        await self.rpc("exit")

    async def ping(self) -> None:
        """Issue a no-op ping command."""
        await self.rpc("ping")

    async def remote_system(self, cmd: str, *, log_output: bool=False) -> \
            Tuple[int, List[bytes]]:
        """
        Run a command on the remote system via system(3).

        :arg cmd:
            Shell command to execute.
        :arg log_output:
            Flag indicating that console log should be collected and returned.
        :returns:
            Tuple (returncode, console_log) where returncode is the exit code
            of the process (negative if killed by signal) and console_log is
            the log of console messages.
        """
        result, console_log = await self.rpc(
                "system {}".format(cmd), log_output=log_output)
        if result is None:
            raise ValueError("expected response object from RPC call")
        status = result["status"]
        if status == "exited":
            returncode = result["code"]
            return returncode, console_log
        elif status == "signaled":
            returncode = -result["signal"]
        else:
            raise BadRequest("unexpected status: {!a}".format(status))
        return returncode, console_log

    async def remote_check_system(self, cmd: str, *, log_output: bool=False) \
            -> List[bytes]:
        """
        Run a command on the remote system via system(3) checking exit code.

        :arg cmd:
            Shell command to execute.
        :arg log_output:
            Flag indicating that console log should be collected and returned.
        :returns:
            log of console messages.
        :raises subprocess.CalledProcessError:
            If the remote command fails or is killed by a signal.
        """
        returncode, console_log = await self.remote_system(
                cmd, log_output=log_output)
        if returncode != 0:
            raise subprocess.CalledProcessError(
                returncode, cmd,
                b''.join(console_log).decode('utf-8') if console_log else None,
                None)
        return console_log

    async def remote_write(self, fname: str, mode: int, data: bytes) \
            -> Dict[Any, Any]:
        """Write a file on the remote system."""
        result, _ = await self.rpc("write {} {:o} {}".format(
            fname, mode, len(data)), data)
        if not isinstance(result, dict):
            raise TypeError("expected RPC call to return a JSON object")
        return result

    async def _drain_console(self, log: Optional[List[bytes]]=None) -> None:
        """Read subsequent console messages until they stop."""
        if self._console is None:
            raise TypeError("console is not ready")
        reader = self._console.reader
        if reader is None:
            raise TypeError("console is not ready for reading")
        while not reader.at_eof():
            line = await reader.readline()
            if line == b'':
                break
            _logger.info("(console) %s", line.rstrip(b"\r\n").decode("utf-8"))
            if log is not None:
                log.append(line.rstrip(b"\r\n"))

    async def _drain_monitor(self, log: Optional[List[bytes]]=None) -> None:
        """Read subsequent QEMU monitor messages until they stop."""
        if self._monitor is None:
            raise TypeError("monitor is not ready")
        reader = self._monitor.reader
        if reader is None:
            raise TypeError("monitor is not ready for reading")
        while not reader.at_eof():
            line = await reader.readline()
            if line == b'':
                break
            _logger.info("(monitor) <- %s", line.rstrip(b"\n").decode("utf-8"))

    async def _drain_testio(self) -> None:
        """Read subsequent test I/O responses until they stop."""
        while await self._read_and_decode_testio() is not None:
            pass

    async def _read_and_decode_testio(self) -> Optional[Dict[Any, Any]]:
        """
        Read and decode a single test I/O response.

        Responses that contain events are automatically acted upon. This is
        done so that we can observe the "boot-ok" event easily.
        """
        if self._testio is None:
            raise TypeError("testio is not ready")
        reader = self._testio.reader
        if reader is None:
            raise TypeError("testio is not ready for reading")
        response_bytes = await reader.readline()
        if response_bytes == b'':
            return None
        response_text = response_bytes.decode('utf-8')
        _logger.info("(test io) <- %s", response_text.rstrip())
        decoded = json.loads(response_text)
        if not isinstance(decoded, dict):
            raise TypeError("expected testio to return serialized JSON object")
        event = decoded.get("event")
        if event == "boot-ok":
            self._booted.set()
        return decoded


_tvm = None  # type: Optional[TestVM]


class VMShellTestCase(unittest.TestCase):
    """Test case class for testing shell scripts in a virtual machine."""

    def _tvm(self) -> TestVM:
        global _tvm
        if _tvm is None:
            raise ValueError("use helpers.main() to prepare test VM")
        return _tvm

    def setUp(self) -> None:
        """
        Prepare for executing each test case.

        This loads the vanilla snapshot and re-sets the mocking and shell
        injection system.
        """
        self.loadvm('vanilla')
        self._sh_lines = []  # type: List[str]
        self._sh_mock_log = "/tmp/mock.log"
        self.sh_inject("rm -f -- {}".format(
            shlex.quote(self._sh_mock_log)))
        super().setUp()

    def savevm(self, name: str) -> None:
        """Save a VM snapshot with the given name."""
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._tvm().savevm(name))

    def loadvm(self, name: str) -> None:
        """Load a VM snapshot with the given name."""
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._tvm().loadvm(name))

    def ping(self) -> None:
        """Issue a no-op ping command."""
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self._tvm().ping())

    def remote_system(self, cmd: str, *, log_output: bool=False) \
            -> Tuple[int, List[bytes]]:
        """Run a command in a virtual machine, via system(3)."""
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
                self._tvm().remote_system(cmd, log_output=log_output))

    def remote_check_system(self, cmd: str, *, log_output: bool=False) \
            -> List[bytes]:
        """Run a command in a virtual machine checking for errors."""
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(
                self._tvm().remote_check_system(cmd, log_output=log_output))

    def remote_write(self, fname: str, mode: int, data: bytes) -> None:
        """Write a file on the remote system."""
        loop = asyncio.get_event_loop()
        resp = loop.run_until_complete(
                self._tvm().remote_write(fname, mode, data))
        self.assertEqual(resp, {"result": "ok", "size": len(data)})

    def remote_write_and_system(self, script: str, *, log_output: bool=False) \
            -> Tuple[int, List[bytes]]:
        """Write a shell script and execute it."""
        self.remote_write("/tmp/command.sh", 0o755,
                          "#!/bin/sh\n{}\n".format(script).encode('utf-8'))
        return self.remote_system("/tmp/command.sh", log_output=log_output)

    def sh_run(self, fn: str) -> Tuple[int, List[bytes]]:
        """Run a shell function."""
        return self.remote_write_and_system(self._sh_text(fn), log_output=True)

    def sh_inject(self, cmd: str) -> None:
        """
        Inject a shell command into the script builder.

        :arg cmd:
            A shell script fragment to inject.

        All injected fragments are stored until they are assembled by
        :meth:`text`. Each fragment should be a valid shell but this is not
        checked or enforced.
        """
        self._sh_lines.append(cmd)

    def sh_source(self, fname: str) -> None:
        """Source another shell script."""
        self.sh_inject(". {}".format(shlex.quote(fname)))

    def sh_mock(self, cmd: str, exits: int=0, returns: int=0,
                prints: Optional[str]=None) -> None:
        """Override any function or program (buffered until execute)."""
        self.sh_inject("""
            {cmd_neutered}() {{
                printf '%s' '{cmd}' >>{mock_log};
                for arg in "$@"; do
                    printf " '%s'" "$arg" >>{mock_log};
                done;
                printf '\\n' >>{mock_log};
                if [ -n "{prints}" ]; then
                    echo "{prints}";
                fi
                if [ {exits} -ne 0 ]; then
                    exit {exits};
                else
                    return {returns};
                fi
            }}
            alias {cmd}="{cmd_neutered}"
        """.format(
            cmd=cmd, cmd_neutered=cmd.replace("-", "_"),
            exits=exits, returns=returns,
            prints=shlex.quote(prints) if prints is not None else "",
            mock_log=shlex.quote(self._sh_mock_log),
        ).strip())

    def sh_mocked_calls(self) -> List[Tuple[str, ...]]:
        """List of calls and arguments to all mocks."""
        return [tuple(shlex.split(line.decode('utf-8')))
                for line in self.remote_check_system(
                    "cat -- {}".format(shlex.quote(self._sh_mock_log)),
                    log_output=True)]

    def _sh_text(self, extra_cmds: str="") -> str:
        """
        Generate the complete script, appending extra commands.

        :returns:
            Each injected fragment of shell, in order, followed by
            the contents of extra_cmds.

        This function is useful to create the full script, ready to be
        executed, while being able to reuse setup operations that are injected
        into the script builder.
        """
        return "\n".join(self._sh_lines) + "\n" + extra_cmds + "\n"


class SmokeTests(VMShellTestCase):
    """Smoke tests for the virtual machine based testing system."""

    def test_ping(self) -> None:
        """Check that ping works."""
        self.ping()

    def test_mount(self) -> None:
        """Check that essential filesystems are mounted."""
        output = self.remote_check_system("mount", log_output=True)
        self.assertRegex(
            output.pop(0).decode(),
            r"rootfs on / type rootfs \(rw,size=[0-9]+k,nr_inodes=[0-9]+\)")
        self.assertEqual(
            output.pop(0).decode(),
            "sysfs on /sys type sysfs (rw,nosuid,nodev,noexec,relatime)")
        self.assertEqual(
            output.pop(0).decode(),
            "proc on /proc type proc (rw,nosuid,nodev,noexec,relatime)")
        self.assertRegex(
            output.pop(0).decode(),
            r"udev on /dev type devtmpfs \(rw,nosuid,relatime,size=[0-9]+k,"
            r"nr_inodes=[0-9]+,mode=755\)")
        self.assertEqual(
            output.pop(0).decode(),
            "devpts on /dev/pts type devpts (rw,nosuid,noexec,relatime,"
            "gid=5,mode=620,ptmxmode=000)")
        self.assertRegex(
            output.pop(0).decode(),
            r"tmpfs on /run type tmpfs \(rw,nosuid,noexec,relatime,"
            r"size=[0-9]+k,mode=755\)")
        self.assertEqual(output, [])

    def test_synchronized_time(self) -> None:
        """Check that the time inside the VM is synchronized with the host."""
        output = self.remote_check_system("date", log_output=True)
        self.assertEqual(len(output), 1)
        now_vm = datetime.datetime.strptime(
                output.pop().decode(), "%a %b %d %H:%M:%S %Z %Y")
        now_here = datetime.datetime.utcnow()
        # Allow for five seconds of delta
        self.assertLess((now_here - now_vm).total_seconds(), 5)

    def test_remote_write(self) -> None:
        """Check that we can write arbitrary binary data."""
        self.remote_write("/tmp/data", 0o644, bytes(range(256)))
        output = self.remote_check_system(
                "cat /tmp/data | wc -c", log_output=True)
        self.assertEqual(output, [b"256"])

    def test_remote_write_and_run(self) -> None:
        """Test we can write remote files."""
        exitcode, output = self.remote_write_and_system("""#!/bin/sh
            echo OK
        """, log_output=True)
        self.assertEqual(exitcode, 0)
        self.assertEqual(output, [b"OK"])

    def test_snapshot_works(self) -> None:
        """Test we can revert to the vanilla snapshot."""
        self.remote_write("/snapshots-are-fun", 0o644, b"")
        output = self.remote_check_system(
            "ls -ld /snapshots-are-fun", log_output=True)
        self.assertEqual(
            output.pop().decode(),
            '-rw-r--r--    1         0 /snapshots-are-fun')
        self.loadvm("vanilla")
        returncode, output = self.remote_system(
            "ls -ld /snapshots-are-fun", log_output=True)
        self.assertEqual(returncode, 1)
        self.assertEqual(
            output.pop().decode(),
            'ls: /snapshots-are-fun: No such file or directory')

    def test_mocking_works(self) -> None:
        self.sh_mock("foo")
        self.sh_run("foo 1 2 3")
        self.assertEqual(self.sh_mocked_calls(), [
            ("foo", "1", "2", "3"),
        ])


def main() -> None:
    """Run unit tests of the current module."""
    # Enable verbose logging if requested
    verbose = False
    for arg in sys.argv:
        if arg == "-v" or arg == "--verbose":
            verbose = True
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    # Prepare a VM for testing
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: None)
    tvm = TestVM()
    try:
        # Make everything
        if loop.run_until_complete(tvm.make_boot_assets()) != 0:
            raise SystemError("cannot make boot assets")
        # Boot the VM
        try:
            loop.run_until_complete(tvm.boot())
        except BootError as exc:
            raise SystemExit(str(exc))
        # Save snapshot after boot
        loop.run_until_complete(tvm.savevm('vanilla'))
        # We are now ready to run tests :-)
        global _tvm
        _tvm = tvm
        unittest.main()
        _tvm = None
    finally:
        loop.run_until_complete(tvm.shutdown())
        tvm.cleanup()


if __name__ == "__main__":
    main()
