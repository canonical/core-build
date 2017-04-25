# System configuration to create bootable Ubuntu Core IoT images using the core snap

The config/ sub directory in this repository contains the source code for the
ubuntu-core-config debian package which ships the system defaults to create a bootable
Ubuntu Core embedded/IoT image using the core snap package.

The initramfs/ sub directory in this repository contains the source code for the
initramfs-tools-ubuntu-core debian package which ships the necessary initramfs addon
scripts on top of the ubuntu initramfs-tools package to manage snaps as rootfs with
writable paths defined in the ubuntu-core-config package.
