#!/usr/bin/env python3
"""
jffs2_image_tool.py

Tools for the 28 image files stored in the first JFFS2 filesystem of the
supplied F133-B ROM.

The important discovery is that the "68-byte records" are standard JFFS2
raw-inode nodes (jffs2_raw_inode).  The JPEG data is fragmented across those
nodes.  A normal JPEG carver that simply copies bytes from FFD8 to FFD9 also
copies the JFFS2 node headers between fragments, producing the horizontal
corruption seen on a PC.  The device's JFFS2 driver reconstructs the file
correctly.

Commands:

  inspect ROM
      List JFFS2 image files, sizes, node counts, dimensions and restart
      intervals.

  extract ROM 0.bin clean.jpg
      Reconstruct a JFFS2 file and remove its 4-byte big-endian application
      length prefix.  The result is a normal JPEG.

  encode TEMPLATE.jpg source.png output.jpg
      Re-encode an image using the template JPEG's quantization tables,
      4:4:4 sampling, Adobe APP14 marker, and restart interval.

  make-nodes TEMPLATE_ROM 0.bin replacement.jpg nodes.bin
      Create JFFS2 raw-inode nodes for a replacement JPEG.  This preserves
      the template file's JFFS2 metadata and increments the inode version.
      The output is a NODE STREAM, not a complete JFFS2 filesystem image.

The final step of inserting nodes into the ROM is intentionally not performed
by this utility. JFFS2 is an append/erase-block filesystem; safe ROM editing
requires choosing erased space and respecting erase-block state.

Requires:
  Python 3
  Pillow 10+ (tested with Pillow 12.3)
"""

import argparse
import io
import struct
import sys
import zlib
from collections import defaultdict
from pathlib import Path

from PIL import Image


JFFS2_MAGIC = 0x1985
JFFS2_NODETYPE_INODE = 0xE002
JFFS2_NODETYPE_DIRENT = 0xE001
JFFS2_NODETYPE_CLEANMARKER = 0x2003
JFFS2_RAW_INODE_SIZE = 68
JFFS2_ERASEBLOCK_DEFAULT = 0x10000

# The first JFFS2 filesystem in the supplied ROM starts here and the next
# cleanmarked JFFS2 filesystem begins at 0xC80000.
DEFAULT_FS_START = 0xBC0000
DEFAULT_FS_END = 0xC80000


def mtd_crc32(data: bytes) -> int:
    """Linux/mtd-utils style CRC32 used by JFFS2."""
    return (zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def u16(b, off):
    return struct.unpack_from("<H", b, off)[0]


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def p16(b, off, value):
    struct.pack_into("<H", b, off, value & 0xFFFF)


def p32(b, off, value):
    struct.pack_into("<I", b, off, value & 0xFFFFFFFF)


def is_valid_inode(data: bytes, pos: int, limit: int) -> bool:
    if pos < 0 or pos + JFFS2_RAW_INODE_SIZE > limit:
        return False
    if u16(data, pos) != JFFS2_MAGIC:
        return False
    if u16(data, pos + 2) != JFFS2_NODETYPE_INODE:
        return False

    totlen = u32(data, pos + 4)
    csize = u32(data, pos + 48)

    if totlen != JFFS2_RAW_INODE_SIZE + csize:
        return False
    if totlen < JFFS2_RAW_INODE_SIZE or pos + totlen > limit:
        return False

    if mtd_crc32(data[pos:pos + 8]) != u32(data, pos + 8):
        return False

    if mtd_crc32(data[pos:pos + 60]) != u32(data, pos + 64):
        return False

    payload = data[pos + 68:pos + 68 + csize]
    if mtd_crc32(payload) != u32(data, pos + 60):
        return False

    return True


class InodeNode:
    def __init__(self, data: bytes, pos: int):
        self.pos = pos
        self.raw = data[pos:pos + JFFS2_RAW_INODE_SIZE]
        self.ino = u32(self.raw, 12)
        self.version = u32(self.raw, 16)
        self.mode = u32(self.raw, 20)
        self.uid = u16(self.raw, 24)
        self.gid = u16(self.raw, 26)
        self.isize = u32(self.raw, 28)
        self.atime = u32(self.raw, 32)
        self.mtime = u32(self.raw, 36)
        self.ctime = u32(self.raw, 40)
        self.offset = u32(self.raw, 44)
        self.csize = u32(self.raw, 48)
        self.dsize = u32(self.raw, 52)
        self.compr = self.raw[56]
        self.usercompr = self.raw[57]
        self.flags = u16(self.raw, 58)
        self.data_crc = u32(self.raw, 60)
        self.node_crc = u32(self.raw, 64)
        self.payload = data[pos + 68:pos + 68 + self.csize]

    @property
    def totlen(self):
        return JFFS2_RAW_INODE_SIZE + self.csize

    def describe(self):
        return {
            "pos": self.pos,
            "ino": self.ino,
            "version": self.version,
            "isize": self.isize,
            "offset": self.offset,
            "csize": self.csize,
            "dsize": self.dsize,
            "compr": self.compr,
        }


def scan_inodes(data: bytes, start=DEFAULT_FS_START, end=DEFAULT_FS_END):
    nodes = []
    pos = start

    while pos + JFFS2_RAW_INODE_SIZE <= end:
        hit = data.find(b"\x85\x19\x02\xe0", pos, end)
        if hit < 0:
            break

        if is_valid_inode(data, hit, end):
            nodes.append(InodeNode(data, hit))

        pos = hit + 2

    return nodes


def scan_dirents(data: bytes, start=DEFAULT_FS_START, end=DEFAULT_FS_END):
    names = {}

    pos = start
    while pos + 40 <= end:
        hit = data.find(b"\x85\x19\x01\xe0", pos, end)
        if hit < 0:
            break

        totlen = u32(data, hit + 4)
        if 40 <= totlen <= end - hit:
            nsize = data[hit + 28]
            if 40 + nsize <= totlen:
                # Validate header and node CRCs.
                header_ok = mtd_crc32(data[hit:hit + 8]) == u32(data, hit + 8)
                node_ok = mtd_crc32(data[hit:hit + 32]) == u32(data, hit + 32)
                name = data[hit + 40:hit + 40 + nsize]
                name_crc = u32(data, hit + 36)
                name_ok = mtd_crc32(name) == name_crc

                if header_ok and node_ok and name_ok:
                    ino = u32(data, hit + 20)
                    try:
                        names[ino] = name.decode("utf-8")
                    except UnicodeDecodeError:
                        names[ino] = name.decode("latin-1")

        pos = hit + 2

    return names


def select_latest_nodes(nodes, ino):
    """Select the latest inode version for every logical file offset."""
    candidates = [n for n in nodes if n.ino == ino]
    by_offset = {}

    for n in candidates:
        old = by_offset.get(n.offset)
        if old is None or n.version > old.version:
            by_offset[n.offset] = n

    return sorted(by_offset.values(), key=lambda n: n.offset)


def reconstruct_file(nodes):
    if not nodes:
        raise ValueError("No inode nodes found.")

    # JFFS2 allows fragments. The stock image files here are uncompressed,
    # so csize == dsize and the payload can be concatenated at logical offset.
    if any(n.compr != 0 or n.csize != n.dsize for n in nodes):
        raise ValueError(
            "Selected file contains compressed JFFS2 data. "
            "This image utility currently expects the uncompressed image files."
        )

    size = max(n.offset + n.dsize for n in nodes)
    out = bytearray(size)

    for n in nodes:
        out[n.offset:n.offset + n.dsize] = n.payload

    return bytes(out[:nodes[0].isize])


def strip_application_prefix(file_data):
    if len(file_data) < 6:
        raise ValueError("File is too small.")

    declared = int.from_bytes(file_data[:4], "big")

    if declared != len(file_data) - 4:
        raise ValueError(
            f"Expected 4-byte big-endian payload length {len(file_data)-4}, "
            f"found {declared}."
        )

    jpeg = file_data[4:]

    if not jpeg.startswith(b"\xFF\xD8"):
        raise ValueError("Payload after length prefix is not JPEG SOI (FFD8).")

    if jpeg.rfind(b"\xFF\xD9") < 0:
        raise ValueError("JPEG has no EOI (FFD9).")

    return jpeg


def add_application_prefix(jpeg):
    if not jpeg.startswith(b"\xFF\xD8"):
        raise ValueError("Input is not a JPEG.")

    return len(jpeg).to_bytes(4, "big") + jpeg


def jpeg_restart_interval(jpeg):
    p = 2

    while p + 4 <= len(jpeg):
        if jpeg[p] != 0xFF:
            break

        while p < len(jpeg) and jpeg[p] == 0xFF:
            p += 1

        marker = jpeg[p]
        p += 1

        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue

        if p + 2 > len(jpeg):
            break

        length = struct.unpack_from(">H", jpeg, p)[0]

        if marker == 0xDD and length >= 4:
            return struct.unpack_from(">H", jpeg, p + 2)[0]

        if marker == 0xDA:
            break

        p += length

    return 0


def jpeg_dimensions(jpeg):
    with Image.open(io.BytesIO(jpeg)) as im:
        return im.size


def extract_app14(jpeg):
    p = 2

    while p + 4 <= len(jpeg):
        if jpeg[p] != 0xFF:
            break

        marker = jpeg[p + 1]

        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            p += 2
            continue

        length = struct.unpack_from(">H", jpeg, p + 2)[0]
        end = p + 2 + length

        if end > len(jpeg):
            break

        if marker == 0xEE and jpeg[p + 4:p + 9] == b"Adobe":
            return jpeg[p:end]

        if marker == 0xDA:
            break

        p = end

    return None


def inject_app14(jpeg, app14):
    if not app14:
        return jpeg

    if not jpeg.startswith(b"\xFF\xD8"):
        raise ValueError("JPEG has no SOI.")

    p = 2

    # Replace the JFIF APP0 emitted by Pillow.
    if p + 4 <= len(jpeg) and jpeg[p:p + 2] == b"\xFF\xE0":
        length = struct.unpack_from(">H", jpeg, p + 2)[0]
        end = p + 2 + length
        return jpeg[:2] + app14 + jpeg[end:]

    return jpeg[:2] + app14 + jpeg[2:]


def encode_like_template(source_path, template_jpeg_path, output_path):
    source_path = Path(source_path)
    template_jpeg_path = Path(template_jpeg_path)

    with Image.open(template_jpeg_path) as tmpl:
        qtables = tmpl.quantization
        dimensions = tmpl.size

    with Image.open(source_path) as src:
        if src.size != dimensions:
            raise ValueError(
                f"Source is {src.size}, template is {dimensions}. "
                "Resize deliberately if a different resolution is intended."
            )

        # The stock images are RGB 4:4:4 and uncompressed at the JFFS2 layer.
        src = src.convert("RGB")

        buf = io.BytesIO()
        src.save(
            buf,
            format="JPEG",
            qtables=qtables,
            subsampling=0,
            optimize=False,
            progressive=False,
            restart_marker_blocks=jpeg_restart_interval(
                template_jpeg_path.read_bytes()
            ),
        )

    generated = buf.getvalue()
    app14 = extract_app14(template_jpeg_path.read_bytes())
    generated = inject_app14(generated, app14)

    Path(output_path).write_bytes(generated)


def make_inode_node(template: InodeNode, payload: bytes, version: int):
    """Build one valid JFFS2 raw inode node from template metadata."""
    b = bytearray(JFFS2_RAW_INODE_SIZE + len(payload))

    p16(b, 0, JFFS2_MAGIC)
    p16(b, 2, JFFS2_NODETYPE_INODE)
    p32(b, 4, JFFS2_RAW_INODE_SIZE + len(payload))

    p32(b, 12, template.ino)
    p32(b, 16, version)
    p32(b, 20, template.mode)
    p16(b, 24, template.uid)
    p16(b, 26, template.gid)

    # isize is patched by the caller after all chunks are known.
    p32(b, 28, template.isize)

    p32(b, 32, template.atime)
    p32(b, 36, template.mtime)
    p32(b, 40, template.ctime)

    # offset is patched by caller.
    p32(b, 44, 0)
    p32(b, 48, len(payload))
    p32(b, 52, len(payload))

    p16(b, 58, template.flags)
    b[56] = template.compr
    b[57] = template.usercompr

    b[68:] = payload

    p32(b, 60, mtd_crc32(payload))
    p32(b, 8, mtd_crc32(b[:8]))
    p32(b, 64, mtd_crc32(b[:60]))

    return b


def make_nodes(template_nodes, jpeg):
    """
    Create a replacement JFFS2 inode-node stream.

    The template's first inode node supplies metadata. New data is divided
    into 0x2000-byte chunks, which is the normal chunk size in the stock
    images. The version counter starts after the highest version in the
    template file.

    The 4-byte application prefix is included in the JFFS2 file contents.
    """
    file_data = add_application_prefix(jpeg)

    if not template_nodes:
        raise ValueError("No template inode nodes.")

    template = template_nodes[0]
    first_version = max(n.version for n in template_nodes) + 1
    chunk_size = 0x2000

    chunks = [
        file_data[i:i + chunk_size]
        for i in range(0, len(file_data), chunk_size)
    ]

    out = bytearray()

    for index, chunk in enumerate(chunks):
        node = make_inode_node(
            template,
            chunk,
            first_version + index,
        )

        p32(node, 28, len(file_data))
        p32(node, 44, index * chunk_size)
        p32(node, 48, len(chunk))
        p32(node, 52, len(chunk))

        # Recalculate CRCs after changing isize/offset.
        p32(node, 60, mtd_crc32(chunk))
        p32(node, 8, mtd_crc32(node[:8]))
        p32(node, 64, mtd_crc32(node[:60]))

        out += node

        # JFFS2 nodes are 4-byte aligned.
        if len(node) & 3:
            out += b"\xFF" * (-len(node) & 3)

    return bytes(out)


def find_inode_by_name(data, name, start, end):
    names = scan_dirents(data, start, end)

    for ino, filename in names.items():
        if filename == name:
            return ino

    raise ValueError(f"Could not find JFFS2 filename {name!r}.")


def command_inspect(args):
    data = Path(args.rom).read_bytes()
    names = scan_dirents(data, args.start, args.end)
    nodes = scan_inodes(data, args.start, args.end)

    by_ino = defaultdict(list)
    for n in nodes:
        by_ino[n.ino].append(n)

    print("inode  filename      JFFS2 nodes  file bytes  JPEG bytes  dimensions  DRI")
    print("-" * 80)

    for ino in sorted(names):
        latest = select_latest_nodes(nodes, ino)
        if not latest:
            continue

        try:
            raw = reconstruct_file(latest)
            jpeg = strip_application_prefix(raw)
            dimensions = jpeg_dimensions(jpeg)
            dri = jpeg_restart_interval(jpeg)
        except Exception:
            continue

        print(
            f"{ino:5d}  {names[ino]:12s} "
            f"{len(latest):11d} {len(raw):11d} {len(jpeg):11d} "
            f"{dimensions!s:11s} {dri}"
        )


def command_extract(args):
    data = Path(args.rom).read_bytes()

    if args.inode is not None:
        ino = args.inode
    else:
        ino = find_inode_by_name(data, args.name, args.start, args.end)

    nodes = select_latest_nodes(scan_inodes(data, args.start, args.end), ino)
    raw = reconstruct_file(nodes)
    jpeg = strip_application_prefix(raw)

    Path(args.output).write_bytes(jpeg)

    print(f"Extracted inode {ino}: {len(jpeg):,} JPEG bytes -> {args.output}")


def command_encode(args):
    encode_like_template(args.source, args.template, args.output)
    print(f"Encoded JPEG -> {args.output}")


def command_make_nodes(args):
    data = Path(args.rom).read_bytes()

    if args.inode is not None:
        ino = args.inode
    else:
        ino = find_inode_by_name(data, args.name, args.start, args.end)

    nodes = select_latest_nodes(scan_inodes(data, args.start, args.end), ino)
    template_jpeg = strip_application_prefix(reconstruct_file(nodes))

    # If the source was already encoded as JPEG, use it directly. Otherwise
    # require the user to run "encode" first so JPEG parameters are explicit.
    jpeg = Path(args.jpeg).read_bytes()
    if not jpeg.startswith(b"\xFF\xD8"):
        raise ValueError("Replacement input must be a JPEG.")

    # Preserve the template's JPEG dimensions as a safety check.
    if jpeg_dimensions(jpeg) != jpeg_dimensions(template_jpeg):
        raise ValueError(
            f"Replacement dimensions {jpeg_dimensions(jpeg)} do not match "
            f"template dimensions {jpeg_dimensions(template_jpeg)}."
        )

    node_stream = make_nodes(nodes, jpeg)
    Path(args.output).write_bytes(node_stream)

    print(
        f"Created {len(node_stream):,} bytes of JFFS2 raw-inode nodes -> "
        f"{args.output}"
    )
    print(
        "This is a node stream, not a complete JFFS2 filesystem and should "
        "not be written to the ROM without a free-space/erase-block plan."
    )


def main():
    p = argparse.ArgumentParser(
        description="Inspect/extract/re-encode JFFS2 JPEG resources."
    )
    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--start", type=lambda x: int(x, 0),
                        default=DEFAULT_FS_START)
    common.add_argument("--end", type=lambda x: int(x, 0),
                        default=DEFAULT_FS_END)

    q = sub.add_parser("inspect", parents=[common])
    q.add_argument("rom")
    q.set_defaults(func=command_inspect)

    q = sub.add_parser("extract", parents=[common])
    q.add_argument("rom")
    q.add_argument("name", nargs="?")
    q.add_argument("output")
    q.add_argument("--inode", type=int)
    q.set_defaults(func=command_extract)

    q = sub.add_parser("encode")
    q.add_argument("template", help="clean template JPEG")
    q.add_argument("source", help="PNG/JPEG/etc.")
    q.add_argument("output")
    q.set_defaults(func=command_encode)

    q = sub.add_parser("make-nodes", parents=[common])
    q.add_argument("rom")
    q.add_argument("name", nargs="?")
    q.add_argument("jpeg")
    q.add_argument("output")
    q.add_argument("--inode", type=int)
    q.set_defaults(func=command_make_nodes)

    args = p.parse_args()

    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
