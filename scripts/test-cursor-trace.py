#!/usr/bin/env python3
"""Exercise the real diagnostic collector and decoder in isolated processes."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cursor_trace_decoder", ROOT / "scripts/decode-cursor-trace.py")
DECODER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DECODER)

PRODUCER = r'''
#define WINE_CURSOR_TRACE_IMPLEMENTATION
#define WINE_CURSOR_TRACE_MODULE "test"
#define WINE_CURSOR_TRACE_CAPACITY 64
#include "wine/cursor_trace.h"
#include <sys/wait.h>

static void *produce(void *arg)
{
    unsigned id = (uintptr_t)arg;
    for (unsigned i = 0; i < 10000; ++i)
        CURSOR_TRACE(WCT_DRIVER, 0, ((uint64_t)id << 32) | i, 0, 0,
                     id, i, id ^ i, -(int)i, 0, 0, 0, 0);
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc != 2) return 1;
    errno = E2BIG;
    if (!strcmp(argv[1], "off"))
    {
        unsigned evaluated = 0;
        CURSOR_TRACE(WCT_DRIVER, 0, ++evaluated, 0, 0, ++evaluated, 0, 0, 0, 0, 0, 0, 0);
        return evaluated || wine_cursor_trace_active || errno != E2BIG;
    }
    if (!wine_cursor_trace_active) return 2;
    if (!strcmp(argv[1], "wrap"))
    {
        for (unsigned i = 0; i < 257; ++i)
            CURSOR_TRACE(WCT_DRIVER, 0, i + 1, 0, 0, i, -((int)i), 0, 0, 0, 0, 0, 0);
    }
    else if (!strcmp(argv[1], "threads"))
    {
        pthread_t threads[8];
        for (unsigned i = 0; i < 8; ++i)
            if (pthread_create(&threads[i], NULL, produce, (void *)(uintptr_t)i)) return 3;
        for (unsigned i = 0; i < 8; ++i) pthread_join(threads[i], NULL);
        CURSOR_TRACE(WCT_DRIVER, 0, (uint64_t)8 << 32, 0, 0, 8, 0, 8, 0, 0, 0, 0, 0);
    }
    else if (!strcmp(argv[1], "fork"))
    {
        int status;
        CURSOR_TRACE(WCT_DRIVER, 0, 1, 0, 0, 42, 0, 0, 0, 0, 0, 0, 0);
        pid_t child = fork();
        if (child < 0) return 4;
        if (!child)
        {
            CURSOR_TRACE(WCT_DRIVER, 0, 2, 0, 0, 43, 0, 0, 0, 0, 0, 0, 0);
            _exit(wine_cursor_trace_active ? 5 : 0);
        }
        if (waitpid(child, &status, 0) != child || status) return 6;
        CURSOR_TRACE(WCT_DRIVER, 0, 3, 0, 0, 44, 0, 0, 0, 0, 0, 0, 0);
    }
    else return 7;
    return errno != E2BIG;
}
'''


class CursorTraceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cursor-trace-tests-")
        cls.root = Path(cls.temporary.name)
        source = cls.root / "producer.c"
        source.write_text(PRODUCER)
        cls.producer = cls.root / "producer"
        subprocess.run(["cc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-I", str(ROOT / "include"), str(source), "-o", str(cls.producer)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_producer(self, mode, enabled=True):
        directory = self.root / self._testMethodName
        directory.mkdir()
        env = dict(os.environ)
        env.pop("YAAGL_CURSOR_TRACE", None)
        if enabled:
            env["YAAGL_CURSOR_TRACE"] = str(directory)
        subprocess.run([str(self.producer), mode], env=env, check=True)
        return directory

    def test_disabled_does_not_evaluate_or_write(self):
        directory = self.run_producer("off", enabled=False)
        self.assertEqual(list(directory.iterdir()), [])

    def test_wrap_retains_exact_latest_records(self):
        directory = self.run_producer("wrap")
        path, = directory.glob("*.bin")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        metadata, events = DECODER.read_trace(path)
        self.assertEqual(metadata["attempted_records"], 257)
        self.assertEqual(metadata["before_retained_window"], 193)
        self.assertEqual(metadata["missing_in_retained_window"], 0)
        self.assertEqual(sorted(event["sequence"] for event in events), list(range(194, 258)))
        for event in events:
            value = event["sequence"] - 1
            self.assertEqual(event["values"]["x"], value)
            self.assertEqual(event["values"]["y"], -value)
            self.assertEqual(event["object"], hex(value + 1))
        event = events[0]
        output = subprocess.check_output([
            "python3", str(ROOT / "scripts/decode-cursor-trace.py"), str(path),
            "--around-ns", str(event["clock_ns"]), "--before", "0", "--after", "0"], text=True)
        import json
        selected = [json.loads(line) for line in output.splitlines()]
        expected = sorted(item["sequence"] for item in events if item["clock_ns"] == event["clock_ns"])
        self.assertEqual([item["sequence"] for item in selected[1:]], expected)

    def test_concurrent_producers_never_publish_torn_payloads(self):
        directory = self.run_producer("threads")
        path, = directory.glob("*.bin")
        metadata, events = DECODER.read_trace(path)
        self.assertEqual(metadata["attempted_records"], 80001)
        self.assertEqual(metadata["unstable_slots"], 0)
        self.assertEqual(len(events) + metadata["missing_in_retained_window"], 64)
        self.assertIn(80001, [event["sequence"] for event in events])
        for event in events:
            identity = int(event["object"], 16)
            thread, index = identity >> 32, identity & 0xffffffff
            self.assertEqual(event["values"]["x"], thread)
            self.assertEqual(event["values"]["y"], index)
            self.assertEqual(event["values"]["raw_x"], thread ^ index)
            self.assertEqual(event["values"]["raw_y"], -index)

    def test_fork_child_does_not_corrupt_parent_stream(self):
        directory = self.run_producer("fork")
        path, = directory.glob("*.bin")
        metadata, events = DECODER.read_trace(path)
        self.assertEqual(metadata["attempted_records"], 2)
        self.assertEqual([event["values"]["x"] for event in events], [42, 44])

    def test_truncated_and_unknown_files_are_rejected(self):
        directory = self.run_producer("wrap")
        path, = directory.glob("*.bin")
        data = path.read_bytes()
        corrupt = directory / "corrupt.bin"
        for content in (data[:-1], b"UNKNOWN!" + data[8:]):
            corrupt.write_bytes(content)
            with self.assertRaises(ValueError):
                DECODER.read_trace(corrupt)


if __name__ == "__main__":
    unittest.main()
