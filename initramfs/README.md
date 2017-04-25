Description
-----------

This package allows selective paths in an Ubuntu Core image (which is
read-only by default) to be made writeable.

This is achieved by bind-mounting a specific, minimal set of paths to a
writable partition.

The file that controls which paths will be made writeable
("``/etc/system-image/writable-paths``") is not provided by this
package, but by the ``ubuntu-core-config`` package.

See Also
--------

This package is based on the ``initramfs-tools-ubuntu-touch`` package.

