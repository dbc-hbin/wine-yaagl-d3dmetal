/*
 * Synthetic-delta correction for direct CGWarpMouseCursorPosition() moves.
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA
 */

#ifndef __WINE_MACDRV_WARPCONSUMPTION_H
#define __WINE_MACDRV_WARPCONSUMPTION_H

#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* Main-thread FIFO. Coordinates are Quartz screen points; times use the
 * systemUptime/NSEvent clock in seconds. EventTap-owned warps stay in its
 * separate tracker and must not be recorded here. */
struct warp_correction_record
{
    double from_x, from_y;
    double to_x, to_y;
    double time_before, time_after;
};

struct warp_correction_state
{
    struct warp_correction_record *records;
    unsigned int count, capacity;
};

static inline void warp_correction_clear(struct warp_correction_state *state)
{
    state->count = 0;
}

static inline void warp_correction_destroy(struct warp_correction_state *state)
{
    free(state->records);
    memset(state, 0, sizeof(*state));
}

/* Reserve BEFORE moving the native cursor. Failure must leave both the cursor
 * and pending corrections intact. Storage is reused; consuming never allocates. */
static inline int warp_correction_reserve(struct warp_correction_state *state)
{
    unsigned int capacity;
    struct warp_correction_record *records;

    if (state->count < state->capacity) return 1;
    if (state->capacity > UINT_MAX / 2) return 0;
    capacity = state->capacity ? state->capacity * 2 : 4;
    if (SIZE_MAX / capacity < sizeof(*records)) return 0;
    if (!(records = realloc(state->records, (size_t)capacity * sizeof(*records)))) return 0;
    state->records = records;
    state->capacity = capacity;
    return 1;
}

/* Caller has reserved a slot and completed a successful native warp. Use its
 * actual before/after locations, not the requested (possibly clamped) target. */
static inline void warp_correction_push(struct warp_correction_state *state,
                                       const struct warp_correction_record *record)
{
    if (record->from_x == record->to_x && record->from_y == record->to_y) return;
    state->records[state->count++] = *record;
}

/* Match the EventTap timestamp/location convention, but retain direct warps
 * across the zero-delta notification observed before their delayed movement.
 * Correct before filtering/routing so a discarded event still retires its warp.
 * Subtract only the summed native displacement, preserving physical movement. */
static inline void warp_correction_apply(struct warp_correction_state *state,
                                        double time, double x, double y, double *dx, double *dy)
{
    unsigned int finished;
    double subtract_x = 0, subtract_y = 0;

    if (!*dx && !*dy) return;
    for (finished = 0; finished < state->count; finished++)
    {
        const struct warp_correction_record *record = &state->records[finished];
        if (!(record->time_after < time ||
              (record->time_before <= time && x == record->to_x && y == record->to_y))) break;
        subtract_x += record->to_x - record->from_x;
        subtract_y += record->to_y - record->from_y;
    }
    if (!finished) return;

    state->count -= finished;
    if (state->count)
        memmove(state->records, state->records + finished, state->count * sizeof(*state->records));
    *dx -= subtract_x;
    *dy -= subtract_y;
}

#endif /* __WINE_MACDRV_WARPCONSUMPTION_H */
