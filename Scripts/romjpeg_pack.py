#!/usr/bin/env python3
"""
romjpeg_pack.py
Experimental pack/unpack utility for the chunked JPEG format found in the
supplied firmware images.

IMPORTANT:
  - The 68-byte records are understood structurally, but several fields are
    still opaque. The packer therefore uses an existing known-good wrapped
    JPEG as a template and preserves the opaque fields.
  - This is intended for analysis/testing, not as a guaranteed production
    firmware encoder.
  - ImageMagick ("magick") is required for PNG/JPEG -> JPEG encoding.

Commands:

  Unwrap:
    python romjpeg_pack.py unpack original_wrapped.jpg plain.jpg

  Pack an existing JPEG without re-encoding:
    python romjpeg_pack.py pack-jpeg plain.jpg original_wrapped.jpg output.jpg

  Convert an image and pack it:
    python romjpeg_pack.py pack new.png original_wrapped.jpg output.jpg

The template should normally be an original image of the same resource type
and preferably the same dimensions.
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image


RECORD_SIZE = 0x44
CHUNK_SIZE = 0x2000
RECORD_SIGNATURE = bytes.fromhex("c0810000ffff9101")


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def put_u32(buf, off, value):
    struct.pack_into("<I", buf, off, value)


def find_records(data):
    """Find and validate the 68-byte records."""
    records = []
    pos = 0

    while True:
        hit = data.find(RECORD_SIGNATURE, pos)
        if hit < 0:
            break

        start = hit - 0x10
        if start < 0 or start + RECORD_SIZE > len(data):
            pos = hit + 1
            continue

        r = data[start:start + RECORD_SIZE]

        # Structural checks greatly reduce the chance of treating JPEG entropy
        # data as a false-positive record.
        chunk_offset = u32(r, 0x28)
        chunk_size_field = u32(r, 0x2C)
        chunk_size_field2 = u32(r, 0x30)
        chunk_index = u32(r, 0x0C)

        if (
            chunk_offset % CHUNK_SIZE == 0
            and chunk_size_field == chunk_size_field2
            and chunk_size_field > 0
            and 0 < chunk_index < 0x10000
        ):
            records.append(start)

        pos = hit + 1

    return records


def unwrap(data):
    """Remove the 68-byte records and return the JPEG payload."""
    records = find_records(data)
    if not records:
        raise ValueError("No valid 68-byte records were found.")

    out = bytearray()
    last = 0

    for start in records:
        out += data[last:start]
        last = start + RECORD_SIZE

    out += data[last:]

    # The payload should now be an ordinary JPEG.
    if not out.startswith(b"\xff\xd8"):
        raise ValueError("Unwrapped data does not start with JPEG SOI (FFD8).")

    eoi = out.rfind(b"\xff\xd9")
    if eoi < 0:
        raise ValueError("Unwrapped data has no JPEG EOI (FFD9).")

    # These supplied files terminate at EOI after the records are removed.
    if eoi + 2 != len(out):
        print(
            f"Warning: {len(out) - (eoi + 2)} bytes occur after JPEG EOI; "
            "preserving them.",
            file=sys.stderr,
        )

    return bytes(out), records


def extract_app14(jpeg):
    """Return the Adobe APP14 segment from a template JPEG, if present."""
    p = 2
    while p + 4 <= len(jpeg):
        if jpeg[p] != 0xFF:
            break

        marker = jpeg[p + 1]

        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            p += 2
            continue

        if p + 4 > len(jpeg):
            break

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


def replace_jfif_with_app14(jpeg, app14):
    """
    ImageMagick normally emits an APP0/JFIF segment. The supplied firmware
    JPEGs instead use Adobe APP14. Replace APP0 with the template's APP14.
    """
    if not app14:
        return jpeg

    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("Generated JPEG does not begin with SOI.")

    p = 2

    if p + 4 <= len(jpeg) and jpeg[p] == 0xFF and jpeg[p + 1] == 0xE0:
        length = struct.unpack_from(">H", jpeg, p + 2)[0]
        end = p + 2 + length
        return jpeg[:2] + app14 + jpeg[end:]

    return jpeg[:2] + app14 + jpeg[2:]


def get_restart_interval(jpeg):
    """Read the JPEG DRI value."""
    p = 2
    while p + 4 <= len(jpeg):
        if jpeg[p] != 0xFF:
            break

        marker = jpeg[p + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            p += 2
            continue

        length = struct.unpack_from(">H", jpeg, p + 2)[0]
        if marker == 0xDD and length >= 4:
            return struct.unpack_from(">H", jpeg, p + 4)[0]

        if marker == 0xDA:
            break

        p += 2 + length

    return 0


def write_qtable_xml(template_jpeg, path):
    """
    ImageMagick's q-table XML accepts the JPEG quantization values in the
    order returned by Pillow for these files. Quality=50 is used below because
    ImageMagick scales custom tables according to -quality; at 50 the supplied
    tables are reproduced exactly for this format.
    """
    im = Image.open(Path(path).with_suffix(".tmp.jpg")) if False else None

    # The caller provides the Pillow-readable template JPEG through the
    # temporary file represented by template_jpeg.
    tpath = Path(template_jpeg)
    pim = Image.open(tpath)
    q = pim.quantization

    if 0 not in q or 1 not in q:
        raise ValueError("Template does not contain both JPEG quantization tables.")

    xml = [
        '<?xml version="1.0" encoding="ISO-8859-1"?>',
        "<quantization-tables>",
    ]

    for slot, alias in ((0, "luminance"), (1, "chrominance")):
        values = ", ".join(str(int(x)) for x in q[slot])
        xml.extend([
            f'<table slot="{slot}" alias="{alias}">',
            f"<description>{alias}</description>",
            '<levels width="8" height="8" divisor="1">',
            values,
            "</levels>",
            "</table>",
        ])

    xml.append("</quantization-tables>")
    Path(path).write_text("\n".join(xml), encoding="utf-8")


def encode_like_template(source_image, template_jpeg, output_jpeg):
    """
    Re-encode an image using the template's quantization tables, sampling
    factor, and restart interval. The result is not claimed to reproduce the
    original Huffman tables byte-for-byte.
    """
    with Image.open(template_jpeg) as tmpl:
        dimensions = tmpl.size
        restart = get_restart_interval(Path(template_jpeg).read_bytes())
        if restart == 0:
            raise ValueError("Template JPEG has no DRI/restart interval.")

        # These supplied images are 4:4:4. Deriving the full SOF sampling
        # factors is unnecessary for the current format, but we enforce the
        # known layout here.
        sampling = "1x1,1x1,1x1"

    with Image.open(source_image) as src:
        if src.size != dimensions:
            raise ValueError(
                f"Source dimensions {src.size} do not match template "
                f"{dimensions}. Resize deliberately before running this tool."
            )

    with tempfile.TemporaryDirectory() as td:
        qxml = Path(td) / "quantization-table.xml"
        raw_out = Path(td) / "encoded.jpg"
        write_qtable_xml(template_jpeg, qxml)

        cmd = [
            "magick",
            str(source_image),
            "-sampling-factor", sampling,
            "-define", f"jpeg:restart-interval={restart}",
            "-define", "jpeg:optimize-coding=false",
            "-define", f"jpeg:q-table={qxml}",
            "-quality", "50",
            str(raw_out),
        ]

        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            raise RuntimeError(
                "ImageMagick 'magick' was not found in PATH. "
                "Install ImageMagick and ensure magick.exe is on PATH."
            )

        generated = raw_out.read_bytes()

    app14 = extract_app14(Path(template_jpeg).read_bytes())
    generated = replace_jfif_with_app14(generated, app14)
    Path(output_jpeg).write_bytes(generated)


def pack_jpeg(jpeg_payload, template_wrapped, output):
    """
    Pack an existing JPEG using the template's 68-byte records.

    Opaque fields (including the 12-byte values at +0x38) are preserved from
    the template. Known structural fields are updated.
    """
    template = Path(template_wrapped).read_bytes()
    template_jpeg, template_records = unwrap(template)

    records = find_records(template)
    if not records:
        raise ValueError("Template contains no recognized records.")

    # Split the new JPEG into 0x2000-byte payload chunks.
    chunks = [
        jpeg_payload[i:i + CHUNK_SIZE]
        for i in range(0, len(jpeg_payload), CHUNK_SIZE)
    ]

    # There is no record before the first 0x2000-byte data chunk.
    # With N data chunks, the observed format therefore has N-1 records.
    if len(chunks) - 1 > len(records):
        raise ValueError(
            f"New JPEG needs {len(chunks) - 1} records, but template has only "
            f"{len(records)}. More samples are needed before safely "
            "generating additional opaque record IDs."
        )

    # Extract template records. Record i describes data chunk i+1.
    template_records_bytes = [
        template[p:p + RECORD_SIZE] for p in records
    ]

    packed = bytearray()
    total_delta = len(jpeg_payload) - len(template_jpeg)

    # First data chunk is emitted without a record.
    first_chunk = chunks[0]
    packed += first_chunk

    # Each following chunk is preceded by its corresponding 68-byte record.
    for i, chunk in enumerate(chunks[1:]):
        tr = bytearray(template_records_bytes[i])

        old_field_size = u32(tr, 0x2C)
        # The final record advertises four bytes more than its physical data
        # payload in the supplied format. Preserve that convention.
        is_final_template_record = (i == len(template_records_bytes) - 1)
        old_chunk_len = old_field_size - (4 if is_final_template_record else 0)

        # Known logical fields.
        put_u32(tr, 0x18, u32(tr, 0x18) + total_delta)
        put_u32(tr, 0x28, (i + 1) * CHUNK_SIZE)

        delta = len(chunk) - old_chunk_len
        put_u32(tr, 0x2C, old_field_size + delta)
        put_u32(tr, 0x30, old_field_size + delta)
        put_u32(tr, 0x00, u32(tr, 0x00) + delta)

        packed += tr
        packed += chunk

    Path(output).write_bytes(packed)


def command_unpack(args):
    data = Path(args.input).read_bytes()
    jpeg, records = unwrap(data)
    Path(args.output).write_bytes(jpeg)
    print(f"Found {len(records)} records.")
    print(f"Unwrapped JPEG: {len(jpeg):,} bytes -> {args.output}")


def command_pack_jpeg(args):
    jpeg = Path(args.input).read_bytes()
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("Input is not a JPEG.")

    pack_jpeg(jpeg, args.template, args.output)
    print(f"Packed JPEG -> {args.output}")


def command_pack(args):
    # First obtain a clean JPEG using the template's JPEG parameters.
    with tempfile.TemporaryDirectory() as td:
        clean = Path(td) / "clean.jpg"
        template_clean = Path(td) / "template_clean.jpg"

        template_data = Path(args.template).read_bytes()
        template_jpeg, _ = unwrap(template_data)
        template_clean.write_bytes(template_jpeg)

        encode_like_template(args.input, template_clean, clean)

        pack_jpeg(clean.read_bytes(), args.template, args.output)

    print(f"Encoded and packed -> {args.output}")


def main():
    parser = argparse.ArgumentParser(
        description="Pack/unpack the observed 68-byte chunked JPEG format."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("unpack", help="remove the 68-byte records")
    p.add_argument("input")
    p.add_argument("output")
    p.set_defaults(func=command_unpack)

    p = sub.add_parser(
        "pack-jpeg",
        help="pack an existing JPEG without re-encoding it",
    )
    p.add_argument("input")
    p.add_argument("template")
    p.add_argument("output")
    p.set_defaults(func=command_pack_jpeg)

    p = sub.add_parser(
        "pack",
        help="encode an image and pack it using a template",
    )
    p.add_argument("input", help="PNG/JPEG/etc.")
    p.add_argument("template", help="known-good wrapped JPEG")
    p.add_argument("output")
    p.set_defaults(func=command_pack)

    args = parser.parse_args()

    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
