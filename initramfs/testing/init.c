/*
 * Copyright (C) 2017 Canonical Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License version 3 as
 * published by the Free Software Foundation.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 */

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/kdev_t.h>
#include <mntent.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/io.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

static void init_logf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
static void init_dief(const char* fmt, ...) __attribute__((noreturn, format(printf, 1, 2)));
static void init_exit_qemu(int code) __attribute__((noreturn));
static void init_early_mount();
static FILE* init_open_testio(const char* testio_devname);
static void init_process_commands(FILE* testio_in, FILE* testio_out);

int main(int argc, char** argv)
{
    // Parse command line arguments to find the name of the test I/O serial
    // port. It should be of the form "testio=ttyS1" and can be provided by
    // appending "-- testio=ttyS1" to the kernel command line.
    const char* testio_devname = NULL;
    for (int i = 0; i < argc; ++i) {
        if (strstr(argv[i], "testio=") == argv[i]) {
            testio_devname = argv[i] + sizeof "testio=" - 1;
        }
    }
    // Prepare testio_in and testio_out that map either to the serial port
    // (when used in python-init testing inside the virtual machine) or to
    // stdin and stdout (respecitvely) when just invoked locally from the build
    // tree.
    FILE* testio_in;
    FILE* testio_out;
    if (testio_devname != NULL) {
        init_early_mount();
        testio_in = testio_out = init_open_testio(testio_devname);
    } else {
        testio_in = stdin;
        testio_out = stdout;
        init_logf("cannot find name of test I/O serial port\n"
                  "please pass it to init using 'testio=ttySxxx' argument\n");
    }
    // Write an event to test I/O to notify python side that we managed to boot
    // successfully. Tests will fail unless this shows up relatively quickly
    // after starting qemu.
    fprintf(testio_out, "{\"event\": \"boot-ok\"}\n");
    // Process commands sent over the test I/O.
    init_process_commands(testio_in, testio_out);
    // Wrap up and close everything.
    fflush(stdout);
    fflush(testio_out);
    // Use a special device mapped to the I/O port to tell qemu to exit.
    init_exit_qemu(0);
    return 0;
}

static void init_cmd_system(const char* cmd, FILE* testio_in, FILE* testio_out)
{
    int status = system(cmd + strlen("system "));
    if (WIFEXITED(status)) {
        fprintf(testio_out, "{\"result\": \"ok\", \"status\": \"exited\", \"code\": %d}\n", WEXITSTATUS(status));
    } else if (WIFSIGNALED(status)) {
        fprintf(testio_out, "{\"result\": \"ok\", \"status\": \"signaled\", \"signal\": %d}\n", WTERMSIG(status));
    }
}

static void init_cmd_write(const char* cmd, FILE* testio_in, FILE* testio_out)
{
    char name[PATH_MAX];
    mode_t mode;
    size_t size;
    if (sscanf(cmd, "write %s %o %zu", name, &mode, &size) < 3) {
        init_dief("cannot parse write command\n");
    }

    int file_fd = open(name, O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, mode);
    if (file_fd < 0) {
        init_dief("cannot open file descriptor %s: %m\n", name);
    }

    FILE* file_stream = fdopen(file_fd, "w");
    if (file_stream == NULL) {
        init_dief("cannot open file stream: %m\n");
    }

    size_t total_read = 0;
    size_t total_wrote = 0;
    char buf[1 << 16];
    while (total_wrote < size) {
        size_t remaining = size - total_wrote;
        size_t to_read = remaining < sizeof buf ? remaining : sizeof buf;
        size_t n_r = fread(buf, 1, to_read, testio_in);
        size_t n_w = fwrite(buf, 1, n_r, file_stream);
        if (n_r != n_w) {
            init_dief("cannot write everything (wrote %zd but expected %zd): %m\n", n_w, n_r);
        }
        total_read += n_r;
        total_wrote += n_w;
    }

    if (fclose(file_stream) < 0) {
        init_dief("cannot close output file: %m\n");
    }
    fprintf(testio_out, "{\"result\": \"ok\", \"size\": %zu}\n", total_wrote);
}

static void init_cmd_shell(const char* cmd, FILE* testio_in, FILE* testio_out)
{
    pid_t child = fork();
    if (child == 0) {
        execl("/bin/sh", "sh", NULL);
        exit(1);
    } else {
        int status;
        if (waitpid(child, &status, 0) < 0) {
            init_dief("cannot wait for child process: %m\n");
        }
        if (WIFEXITED(status)) {
            fprintf(testio_out, "{\"result\": \"ok\", \"status\": \"exited\", \"code\": %d}\n", WEXITSTATUS(status));
        } else if (WIFSIGNALED(status)) {
            fprintf(testio_out, "{\"result\": \"ok\", \"status\": \"signaled\", \"signal\": %d}\n", WTERMSIG(status));
        } else if (WIFSTOPPED(status)) {
            fprintf(testio_out, "{\"result\": \"ok\", \"status\": \"stopped\", \"signal\": %d}\n", WSTOPSIG(status));
            // We don't want stopped processes, kill them.
            kill(child, SIGKILL);
            wait(NULL);
        }
    }
}

static void init_process_commands(FILE* testio_in, FILE* testio_out)
{
    char* cmd = NULL;
    size_t cmd_cap = 0;
    ssize_t cmd_len = 0;
    bool again = true;
    while (again) {
        // Get a command and chomp the trailing newline.
        if ((cmd_len = getline(&cmd, &cmd_cap, testio_in)) < 0) {
            init_dief("cannot read command: %m\n");
        }
        if (cmd_len > 0 && cmd[cmd_len - 1] == '\n') {
            cmd[cmd_len - 1] = '\0';
            cmd_len -= 1;
        }
        // Process commands:
        if (strcmp(cmd, "") == 0) {
        } else if (strcmp(cmd, "exit") == 0) {
            // exit  - stop the command parse
            again = false;
            fprintf(testio_out, "{\"result\": \"ok\"}\n");
        } else if (strcmp(cmd, "ping") == 0) {
            // ping  - reply with a pong
            fprintf(testio_out, "{\"result\": \"ok\"}\n");
        } else if (strstr(cmd, "system ") == cmd) {
            // write - write a file at any path, with any permissions
            init_cmd_system(cmd, testio_in, testio_out);
        } else if (strstr(cmd, "write ") == cmd) {
            // run   - run a shell command
            init_cmd_write(cmd, testio_in, testio_out);
        } else if (strcmp(cmd, "shell") == 0) {
            // shell - spawn a shell attached to test I/O, for interactive debugging
            init_cmd_shell(cmd, testio_in, testio_out);
        } else {
            fprintf(testio_out, "{\"result\": \"bad-request\"}\n");
        }
        fflush(testio_out);
    }
    if (cmd != NULL) {
        free(cmd);
    }
}

static FILE* init_open_testio(const char* testio_devname)
{
    char testio_path[PATH_MAX];
    if (snprintf(testio_path, sizeof testio_path, "/dev/%s", testio_devname) >= sizeof testio_path) {
        init_dief("cannot format path to test I/O serial port: %m\n");
    }
    int testio_fd = open(testio_path, O_RDWR | O_NOCTTY | O_SYNC | O_CLOEXEC);
    if (testio_fd < 0) {
        init_dief("cannot open serial port %s: %m\n", testio_path);
    }
    // Enable exclusive mode on the testio serial port. In case some tests
    // accidentally tries to use it and clobber the python-init interaction.
    if (ioctl(testio_fd, TIOCEXCL) < 0) {
        init_dief("cannot enable exclusive access mode on serial port: %m\n");
    }
    // Switch the serial port into raw mode.
    struct termios t;
    if (ioctl(testio_fd, TCGETS, &t) < 0) {
        init_dief("cannot get serial port settings: %m\n");
    }
    cfmakeraw(&t);
    if (ioctl(testio_fd, TCSETS, &t) < 0) {
        init_dief("cannot set serial port settings: %m\n");
    }
    // Wrap the file descriptor in FILE for convenience.
    FILE* f = fdopen(testio_fd, "rb+");
    if (f == NULL) {
        init_dief("cannot open test I/O device (stream): %m\n");
    }
    setvbuf(f, NULL, _IONBF, 0);
    return f;
}

static void init_mkdir(const char* dir, mode_t mode)
{
    if (mkdir(dir, mode) < 0 && errno != EEXIST) {
        init_dief("cannot create directory %s: %m\n", dir);
    }
}

static void init_mount(const char* source, const char* target, const char* filesystemtype, unsigned long mountflags, const void* data)
{
    if (mount(source, target, filesystemtype, mountflags, data) < 0) {
        init_dief("cannot mount %s at %s (type %s): %m\n", source, target, filesystemtype);
    }
}

static void init_symlink_sf(const char* target, const char* linkpath)
{
    if (access(linkpath, F_OK) == 0) {
        if (unlink(linkpath) < 0) {
            init_dief("cannot unlink %s: %m\n", linkpath);
        }
    }
    if (symlink(target, linkpath) < 0) {
        init_dief("cannot symlink %s -> %s: %m\n", target, linkpath);
    }
}

static void init_mknod(const char* pathname, mode_t mode, dev_t dev)
{
    if (mknod(pathname, mode, dev) < 0) {
        init_dief("cannot mknod %s (mode %o, dev: %lu:%lu): %m\n",
            pathname, mode, dev >> 8, dev & 255);
    }
}

static void init_early_mount()
{
    init_mkdir("/dev", 0755);
    init_mkdir("/root", 0700);
    init_mkdir("/sys", 0755);
    init_mkdir("/proc", 0755);
    init_mkdir("/tmp", 0755);
    init_mkdir("/var/lock", 0755);
    init_mount("sysfs", "/sys", "sysfs", MS_NODEV | MS_NOEXEC | MS_NOSUID, NULL);
    init_mount("proc", "/proc", "proc", MS_NODEV | MS_NOEXEC | MS_NOSUID, NULL);
    init_symlink_sf("/proc/mounts", "/etc/mtab");
    if (mount("udev", "/dev", "devtmpfs", MS_NOSUID, "mode=0755") < 0) {
        init_mount("udev", "/dev", "tmpfs", MS_NOSUID, "mode=0755");
        init_mknod("/dev/console", 0600 | S_IFCHR, MKDEV(1, 5));
        init_mknod("/dev/null", 0666 | S_IFCHR, MKDEV(1, 3));
    }
    init_mkdir("/dev/pts", 0755);
    init_mount("devpts", "/dev/pts", "devpts", MS_NOEXEC | MS_NOSUID, "gid=5,mode=0620");
    init_mount("tmpfs", "/run", "tmpfs", MS_NOEXEC | MS_NOSUID, "size=10%,mode=0755");
    init_mkdir("/run/initramfs", 0755);
}

static void init_dief(const char* fmt, ...)
{
    printf("test-init, fatal error: ");
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
    init_exit_qemu(1);
}

static void init_logf(const char* fmt, ...)
{
    printf("test-init: ");
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

static void init_exit_qemu(int code)
{
    // Allow access into space of IO ports at the address of isa-debug-exit
    // device that is exposed by QEMU.
    if (ioperm(0xf4, sizeof(long) * 8, 1) < 0) {
        init_logf("cannot set IO permissions: %m\n");
        goto out;
    };
    // Write the exit code to the IO port.
    outl(code, 0xf4);
out:
    init_logf("cannot exit qemu from the guest, exiting/crashing init\n");
    exit(0);
}
