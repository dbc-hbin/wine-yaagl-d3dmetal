/*
 * Shared msync Mach mapping protocol.
 *
 * The client and wineserver may have different native VM page sizes.  Keep
 * the object-page granularity fixed in the wire protocol.
 */

#ifndef __WINE_WINE_MSYNC_H
#define __WINE_WINE_MSYNC_H

#define MSYNC_SHM_WIRE_VERSION 2u
#define MSYNC_SHM_PAGE_SIZE 16384u
#define MSYNC_SHM_OBJECT_SIZE 16u
#define MSYNC_SHM_INDEX_BITS 28u
#define MSYNC_SHM_INDEX_COUNT (1u << MSYNC_SHM_INDEX_BITS)
#define MSYNC_SHM_INDEX_MASK (MSYNC_SHM_INDEX_COUNT - 1)
#define MSYNC_SHM_CLOSE_FLAG MSYNC_SHM_INDEX_COUNT
#define MSYNC_SHM_OBJECTS_PER_PAGE (MSYNC_SHM_PAGE_SIZE / MSYNC_SHM_OBJECT_SIZE)
#define MSYNC_SHM_MAX_PAGES (MSYNC_SHM_INDEX_COUNT / MSYNC_SHM_OBJECTS_PER_PAGE)

#endif /* __WINE_WINE_MSYNC_H */
