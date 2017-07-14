# What the python side cares about

.PHONY: all

all: kernel initrd disk.img

kernel: kernel-snap/kernel.img
	ln -sf $^ $@

initrd: initrd.back-to-back.cpio.gz
	ln -sf $^ $@

disk.img:
	qemu-img create -q -f qcow2 $@ 1G

.PHONY: clean
clean:
	rm -f init *.o
	rm -f initrd.test-extras.cpio.gz
	rm -f initrd.back-to-back.cpio.gz
	rm -f initrd.vanilla.cpio.gz
	rm -f disk.img
	rm -f kernel initrd
	rm -rf kernel-snap core-snap

# How to download snaps

core.snap:
	snap download core
	ln -sf `ls core_*.snap | sort | tail -1` $@

pc-kernel.snap:
	snap download pc-kernel
	ln -sf `ls pc-kernel_*.snap | sort | tail -1` $@

# How to unpack initrd and linux image

core-snap/boot/initrd.img-core: core.snap
	rm -rf core-snap
	unsquashfs -dest core-snap -no-progress $^ '/boot/initrd.img*' >/dev/null
	touch $@

kernel-snap/kernel.img: pc-kernel.snap
	rm -rf kernel-snap
	unsquashfs -dest kernel-snap -no-progress $^ '/kernel.img' >/dev/null
	touch $@

# How to build augmented initrd with special init program

# The test-extras initrd contains the special init program
initrd.test-extras.cpio.gz: init $(sort $(shell find ../scripts -type f))
	ls $^ | cpio --quiet --create --owner=0:0 --format=newc | gzip > $@

initrd.back-to-back.cpio.gz:: initrd.vanilla.cpio.gz
	dd if=$< of=$@ conv=notrunc status=none

# The back-to-back initrd contains the concatenation of both initrd's
# with some small optimizations to make the common case faster.
initrd.back-to-back.cpio.gz:: initrd.test-extras.cpio.gz | initrd.vanilla.cpio.gz
	dd if=$< of=$@ seek=`stat -c %s $|` bs=1 conv=notrunc status=none
	truncate --size=$$(expr $$(stat -c %s $|) + $$(stat -c %s $<)) $@

# The vanilla initrd is just the recomopressed initrd from the core snap.
initrd.vanilla.cpio.gz: core-snap/boot/initrd.img-core
	lzcat < $< | gzip > $@

# How to build the init program

CFLAGS += -Wall -Werror -g -ggdb3

init: init.c
	$(CC) $(CFLAGS) $(CPPFLAGS) $(LDFLAGS) -o $@ $^ $(LDLIBS)

.PHONY: fmt
fmt: init.c
	clang-format -i -style=WebKit $^
