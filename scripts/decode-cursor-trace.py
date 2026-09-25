#!/usr/bin/env python3
"""Read bounded cursor diagnostic rings without changing the running process."""

import argparse
import collections
import json
import math
import os
from pathlib import Path
import struct
import sys

HEADER = struct.Struct("<8s4I5Q16s48s")
RECORD = struct.Struct("<5QdQII8d")
SEQUENCE = struct.Struct("<Q")
KINDS = (
    "", "clip", "set_pos", "warp", "warp_match", "tap", "cocoa", "filter",
    "queue_new", "queue_merge", "queue_drop", "queue_take", "driver", "accum",
    "send", "register", "raw_read", "raw_buffer", "cursor_pos", "focus",
    "display", "geometry", "app_message",
)
FLAGS = {
    1: "before", 2: "after", 4: "success", 8: "noop", 16: "retina",
    32: "confinement", 64: "event_tap", 128: "old", 256: "zero",
    512: "absolute", 1024: "size_only", 2048: "header_only", 4096: "error",
    8192: "host_query", 16384: "source_ns", 32768: "source_ms",
    0x80000000: "transition",
}
FIELDS = {
    "clip": "x y width height enabled reset unused6 unused7",
    "set_pos": "x y clipping unused3 unused4 unused5 unused6 unused7",
    "warp": "from_x from_y to_x to_y before_ns_or_error unused5 unused6 unused7",
    "warp_match": "from_x from_y to_x to_y before_ns after_ns incoming_dx incoming_dy",
    "tap": "dx dy x y matched_warps pending_warps unused6 unused7",
    "cocoa": "dx dy cg_x cg_y cutoff_seconds force_absolute scale event_type",
    "filter": "dx dy cutoff_seconds unused3 unused4 unused5 unused6 unused7",
    "queue_new": "x y raw_x raw_y remainder_x remainder_y scale noncoalescible",
    "queue_merge": "left_raw_x left_raw_y right_raw_x right_raw_y sum_raw_x sum_raw_y left_type right_type",
    "queue_drop": "x y raw_x raw_y event_type unused5 unused6 unused7",
    "queue_take": "x y raw_x raw_y event_type unused5 unused6 unused7",
    "driver": "x y raw_x raw_y input_flags drag unused6 unused7",
    "accum": "x y raw_x raw_y sample_index sample_count input_flags pending_count",
    "send": "x y raw_x raw_y sample_index sample_count input_flags send_flags",
    "register": "device_flags usage_page usage unused3 unused4 unused5 unused6 unused7",
    "raw_read": "dx dy mouse_flags button_flags extra_information wparam size unused7",
    "raw_buffer": "dx dy mouse_flags button_flags sample_index sample_count size extra_information",
    "cursor_pos": "x y raw_x raw_y age_ms dpi_num dpi_den unused7",
    "focus": "active retina cutoff_seconds force_absolute unused4 unused5 unused6 unused7",
    "display": "old_retina new_retina change_count unused3 unused4 unused5 unused6 unused7",
    "geometry": "screen_index x y width height scale screen_count unused7",
    "app_message": "message screen_x screen_y lparam_x lparam_y remove origin unused7",
}


def read_trace(path):
    """Sequence-before/body/sequence-after rejects in-flight or replaced slots.

    A live ring is not a globally atomic snapshot. Metadata explicitly reports
    concurrent writes and missing slots; it never silently repairs the stream.
    """
    records = []
    with path.open("rb") as stream:
        fd = stream.fileno()
        raw = os.pread(fd, HEADER.size, 0)
        if len(raw) != HEADER.size:
            raise ValueError(f"{path}: truncated header")
        magic, version, size, capacity, header_size, pid, start, last, dropped, epoch, module, _ = HEADER.unpack(raw)
        if magic != b"YACUR01\0" or version != 1 or size != RECORD.size or header_size != HEADER.size:
            raise ValueError(f"{path}: unsupported trace format")
        if not 1 <= capacity <= 65536 or os.fstat(fd).st_size != header_size + capacity * size:
            raise ValueError(f"{path}: invalid capacity or file length")
        module = module.rstrip(b"\0").decode("ascii")
        stream_id = f"{pid}:{module}:{start}"
        lower = max(1, last - capacity + 1)
        unstable = 0
        for index in range(capacity):
            offset = header_size + index * size
            before = os.pread(fd, 8, offset)
            body = os.pread(fd, size, offset)
            after = os.pread(fd, 8, offset)
            if len(body) != size or len(before) != 8 or len(after) != 8:
                raise ValueError(f"{path}: truncated while reading")
            sequence = SEQUENCE.unpack(before)[0]
            if before != after or before != body[:8] or sequence == (1 << 64) - 1:
                unstable += 1
                continue
            if not sequence or sequence < lower or sequence > last:
                continue
            if (sequence - 1) % capacity != index:
                raise ValueError(f"{path}: sequence in wrong ring slot")
            seq, clock, thread, obj, related, source_time, generation, kind, flags, *values = RECORD.unpack(body)
            if not 0 < kind < len(KINDS):
                raise ValueError(f"{path}: unknown event kind {kind}")
            name = KINDS[kind]
            fields = FIELDS[name].split()
            if name == "set_pos" and module == "win32u":
                fields = "x y server_old_x server_old_y server_new_x server_new_y requested_raw_x requested_raw_y".split()
            elif name == "send" and flags & 2:
                fields[4] = "status"
            elif name == "raw_buffer" and flags & 2:
                fields = "count next_size header_size capacity unused4 unused5 unused6 unused7".split()
            elif name == "raw_read" and flags & (1024 | 2048 | 4096):
                fields = "unused0 unused1 command size unused4 unused5 unused6 unused7".split()
            payload = {key: value if math.isfinite(value) else str(value)
                       for key, value in zip(fields, values) if not key.startswith("unused")}
            records.append({
                "stream": stream_id, "pid": pid, "module": module,
                "sequence": seq, "clock_ns": clock, "thread": thread,
                "epoch": generation, "kind": name,
                "flags": [label for bit, label in FLAGS.items() if flags & bit],
                "object": hex(obj), "related": hex(related),
                "source_time": source_time if math.isfinite(source_time) else str(source_time),
                "source_unit": "ns" if flags & 16384 else "ms" if flags & 32768 else "seconds_or_none",
                "values": payload,
            })
        end_header = os.pread(fd, HEADER.size, 0)
        if len(end_header) != HEADER.size:
            raise ValueError(f"{path}: truncated during capture")
        end_last = HEADER.unpack(end_header)[7]
        metadata = {
            "kind": "trace_metadata", "path": str(path), "stream": stream_id,
            "pid": pid, "module": module, "capacity": capacity,
            "attempted_records": last, "contention_drops_at_start": dropped,
            "before_retained_window": max(0, last - capacity),
            "missing_in_retained_window": min(last, capacity) - len(records),
            "unstable_slots": unstable, "live_writes_during_read": end_last != last,
            "epoch_at_start": epoch,
            "correlation": "object identities are stream-local; no automatic cross-process causal join",
        }
    return metadata, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="trace files or directories")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--last-seconds", type=float)
    selection.add_argument("--around-ns", type=int, help="CLOCK_MONOTONIC timestamp from an event")
    parser.add_argument("--before", type=float, default=2)
    parser.add_argument("--after", type=float, default=1)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    for value in (args.before, args.after, args.last_seconds):
        if value is not None and (not math.isfinite(value) or value < 0):
            parser.error("time windows must be finite and nonnegative")
    files = sorted({file for path in args.paths
                    for file in (path.glob("cursor-*.bin") if path.is_dir() else [path])})
    if not files:
        parser.error("no cursor trace files found")
    metadata, records = [], []
    try:
        for path in files:
            info, events = read_trace(path)
            metadata.append(info)
            records.extend(events)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(1, f"{error}\n")
    records.sort(key=lambda event: (event["clock_ns"], event["stream"], event["sequence"]))
    if args.last_seconds is not None and records:
        lower = records[-1]["clock_ns"] - args.last_seconds * 1e9
        records = [event for event in records if event["clock_ns"] >= lower]
    elif args.around_ns is not None:
        lower = args.around_ns - args.before * 1e9
        upper = args.around_ns + args.after * 1e9
        records = [event for event in records if lower <= event["clock_ns"] <= upper]
    for info in metadata:
        print(json.dumps(info, allow_nan=False))
    if args.summary:
        counts = collections.Counter(event["kind"] for event in records)
        bad_merges = []
        for event in records:
            if event["kind"] == "queue_merge":
                values = event["values"]
                if (values["left_raw_x"] + values["right_raw_x"] != values["sum_raw_x"] or
                    values["left_raw_y"] + values["right_raw_y"] != values["sum_raw_y"]):
                    bad_merges.append({"stream": event["stream"], "sequence": event["sequence"]})
        print(json.dumps({"kind": "summary", "event_counts": counts,
                          "merge_sum_mismatches": bad_merges,
                          "note": "raw_read observations may repeat the same handle; they are not automatically additional physical input"}))
    else:
        for event in records:
            print(json.dumps(event, allow_nan=False))


if __name__ == "__main__":
    main()
