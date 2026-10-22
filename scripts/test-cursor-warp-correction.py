#!/usr/bin/env python3
"""Compile production warp bookkeeping and exercise input-preservation boundaries."""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

DRIVER = r'''
#include <assert.h>
#include <stdlib.h>

static int fail_allocation;
static void *test_realloc(void *ptr, size_t size)
{
    return fail_allocation ? NULL : realloc(ptr, size);
}
#define realloc test_realloc
#include "cocoa_warpconsumption.h"
#undef realloc

static struct warp_correction_state state;

static void record(double fx, double fy, double tx, double ty, double before, double after)
{
    struct warp_correction_record r = {fx, fy, tx, ty, before, after};
    assert(warp_correction_reserve(&state));
    warp_correction_push(&state, &r);
}

static void movement(double time, double dx, double dy, double x, double y,
                     double expected_x, double expected_y)
{
    double sx, sy;
    unsigned int finished = warp_correction_match(&state, time, dx, dy, x, y);
    warp_correction_consume(&state, finished, &sx, &sy);
    assert(dx - sx == expected_x);
    assert(dy - sy == expected_y);
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "capture"))
    {
        /* Captured: zero notification followed by delayed fold plus (1,-2).
           Do not discard the physical residual or correct the next event twice. */
        record(1636, 674, 960, 540, 100, 100.005);
        movement(100.006, 0, 0, 960, 540, 0, 0);
        movement(100.257, -675, -136, 960, 540, 1, -2);
        movement(100.3, 4, -3, 960, 540, 4, -3);
    }
    else if (!strcmp(argv[1], "ordered"))
    {
        record(0, 0, 10, 10, 5, 5.1);
        record(10, 10, 30, 30, 5.2, 5.3);
        movement(4.9, 3, 4, 3, 4, 3, 4);          /* queued before both warps */
        movement(5.15, 11, 12, 10, 10, 1, 2);   /* only first fold arrived */
        movement(5.5, 20.5, 19.5, 30, 30, .5, -.5);
    }
    else if (!strcmp(argv[1], "bracketed"))
    {
        record(100, 100, 200, 200, 10, 10.1);
        movement(10.01, 0, 0, 200, 200, 0, 0);  /* zero at destination */
        movement(10.02, 5, 0, 50, 50, 5, 0);   /* still pre-warp location */
        movement(10.05, 100.25, 99.5, 200, 200, .25, -.5);
    }
    else if (!strcmp(argv[1], "fractional"))
    {
        record(.5, .25, 2.25, 1, 1, 1.05);
        record(2.25, 1, 2.25, 4.5, 1.1, 1.2);
        movement(2, 1.875, 4, 2.25, 4.5, .125, -.25);
        movement(3, -.125, .25, 2.25, 4.5, -.125, .25);
    }
    else if (!strcmp(argv[1], "noop"))
    {
        record(100, 100, 100, 100, 1, 1.1);
        movement(1.15, 4, 2, 100, 100, 4, 2);
        record(100, 100, 100, 150, 1.2, 1.3);
        record(100, 150, 100, 150, 1.4, 1.5);
        movement(2, 4, 52, 100, 150, 4, 2);
    }
    else if (!strcmp(argv[1], "clear"))
    {
        record(0, 0, 10, 10, 5, 5.1);
        warp_correction_clear(&state);  /* ownership lost; old fold is invalid */
        movement(6, 10, 10, 30, 30, 10, 10);
        record(30, 30, 40, 30, 7, 7.1);
        movement(8, 11, -2, 40, 30, 1, -2);
    }
    else if (!strcmp(argv[1], "allocation"))
    {
        unsigned int i, pending;
        fail_allocation = 1;
        assert(!warp_correction_reserve(&state));
        movement(1, 2, 3, 0, 0, 2, 3);  /* failed reserve cannot arm correction */
        fail_allocation = 0;
        record(0, 0, 1, 0, 2, 2.1);
        pending = state.capacity;
        for (i = 1; i < pending; i++) record(i, 0, i+1, 0, 2+i, 2.1+i);
        fail_allocation = 1;
        assert(!warp_correction_reserve(&state));
        movement(100, pending + .25, -.5, pending, 0, .25, -.5);
    }
    else if (!strcmp(argv[1], "growth"))
    {
        unsigned int i;
        for (i = 0; i < 20; i++) record(i, 0, i+1, 0, 2*i, 2*i+.1);
        movement(40, 20.5, -.25, 20, 0, .5, -.25);
        movement(41, 2, -3, 20, 0, 2, -3);
    }
    else return 2;
    warp_correction_destroy(&state);
    return 0;
}
'''


class WarpCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="warp-correction-tests-")
        root = Path(cls.temporary.name)
        source = root / "driver.c"
        source.write_text(DRIVER)
        cls.driver = root / "driver"
        subprocess.run(["cc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "dlls/winemac.drv"), str(source),
                        "-o", str(cls.driver)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_capture_preserves_real_movement_after_zero_notification(self):
        subprocess.run([str(self.driver), "capture"], check=True)

    def test_queued_events_consume_only_preceding_warps(self):
        subprocess.run([str(self.driver), "ordered"], check=True)

    def test_event_inside_warp_interval_requires_matching_location(self):
        subprocess.run([str(self.driver), "bracketed"], check=True)

    def test_successive_warps_preserve_fractional_movement(self):
        subprocess.run([str(self.driver), "fractional"], check=True)

    def test_noop_does_not_subtract_real_movement(self):
        subprocess.run([str(self.driver), "noop"], check=True)

    def test_clear_prevents_stale_correction_after_ownership_change(self):
        subprocess.run([str(self.driver), "clear"], check=True)

    def test_allocation_failure_preserves_pending_corrections(self):
        subprocess.run([str(self.driver), "allocation"], check=True)

    def test_queue_growth_does_not_lose_warps(self):
        subprocess.run([str(self.driver), "growth"], check=True)


if __name__ == "__main__":
    unittest.main()
