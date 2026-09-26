#!/bin/sh
# Build a minimal x86_64 Linux guest for NetBBS VM doors from Alpine's own
# packages: Alpine's "virt" kernel, a statically linked BusyBox, the four 9p
# modules and the ACPI power button, with examples/doors/vm/init as PID 1.
#
#   sh build-alpine-guest.sh OUTDIR
#
# Produces OUTDIR/vmlinux (uncompressed, booted directly by qemu -- faster than
# the compressed image, which matters under software emulation) and
# OUTDIR/initrd.cpio. Needs curl, tar, gzip, python3 and pax or cpio. Runs on
# NetBSD and Linux; nothing is installed on the host.
#
# This is a recipe, not a supported image: what it downloads, and keeping it
# patched, is the SysOp's. Rebuild it to pick up Alpine's kernel updates.
set -eu
out=${1:?usage: build-alpine-guest.sh OUTDIR}
mirror=${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}
branch=${ALPINE_BRANCH:-latest-stable}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"
cd "$work"

repo=$mirror/$branch/main/x86_64
curl -fsSL -o APKINDEX.tar.gz "$repo/APKINDEX.tar.gz"
tar -xzf APKINDEX.tar.gz APKINDEX
version() {
    awk -v want="$1" '/^P:/ { name = substr($0, 3) } /^V:/ && name == want { print substr($0, 3); exit }' APKINDEX
}
for package in linux-virt busybox-static; do
    file=$package-$(version $package).apk
    echo "fetching $file"
    curl -fsSL -o "$file" "$repo/$file"
    mkdir -p "$package"
    # An .apk is gzip-compressed tar segments; tar warns about the signature.
    tar -xzf "$file" -C "$package" 2>/dev/null || true
done

root=$work/root
mkdir -p "$root/bin" "$root/lib/modules" "$root/proc" "$root/sys" "$root/dev" "$root/mnt" "$root/tmp"
cp busybox-static/bin/busybox.static "$root/bin/busybox"
cp "$here/init" "$root/init"
chmod 755 "$root/init" "$root/bin/busybox"
modules=$(ls -d linux-virt/lib/modules/*)/kernel
for module in fs/netfs/netfs net/9p/9pnet net/9p/9pnet_virtio fs/9p/9p drivers/acpi/tiny-power-button; do
    gzip -dc "$modules/$module.ko.gz" > "$root/lib/modules/$(basename $module).ko"
done

# The PVH entry point qemu boots directly lives in the ELF inside the bzImage.
python3 - linux-virt/boot/vmlinuz-virt "$out/vmlinux" <<'EOF'
import sys, zlib
data = open(sys.argv[1], "rb").read()
start = data.find(b"\x1f\x8b\x08\x00")
open(sys.argv[2], "wb").write(zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[start:]))
EOF
if command -v pax >/dev/null 2>&1; then
    (cd "$root" && find . | pax -w -x sv4cpio) > "$out/initrd.cpio"
else
    (cd "$root" && find . | cpio -o -H newc) > "$out/initrd.cpio"
fi
echo "built $out/vmlinux and $out/initrd.cpio"
