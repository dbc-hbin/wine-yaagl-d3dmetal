/*
 * Mac graphics driver hooks used by D3DMetal (part of the Apple Game Porting Toolkit)
 *
 * Copyright 2025 Brendan Shanks for CodeWeavers, Inc.
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

#if defined(__x86_64__)

#include "config.h"
#include "macdrv.h"

#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>

#import "cocoa_app.h"
#import "cocoa_event.h"
#import "cocoa_window.h"
#import "d3dmetal_objc.h"

#pragma GCC diagnostic ignored "-Wdeclaration-after-statement"

static void *macdrv_retain_d3dmetal_content_view(WineContentView *view)
{
    return [(id)view retain];
}

void macdrv_release_d3dmetal_content_view(WineContentView *view)
{
    OnMainThread(^{ [(id)view release]; });
}

/* D3DMetal retains the snapshot's opaque client_cocoa_view and releases only
 * that object at swapchain destruction. Do not expose the real NSView here. */
@interface WineD3DMetalViewOwner : NSObject
{
@public
    void *context;
}
@end

@implementation WineD3DMetalViewOwner
- (void) dealloc
{
    void *state = context;

    /* Surface detach also synchronously dispatches to Cocoa. OnMainThread()
     * always queues and waits, so all cleanup must leave the main thread. */
    if ([NSThread isMainThread])
        dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
            macdrv_destroy_d3dmetal_view_owner(state);
        });
    else
    {
        macdrv_destroy_d3dmetal_view_owner(state);
    }
    [super dealloc];
}
@end

void *macdrv_create_d3dmetal_view_owner(void *context, WineContentView *view)
{
    WineD3DMetalViewOwner *owner = [[WineD3DMetalViewOwner alloc] init];
    if (owner)
    {
        owner->context = context;
        /* A client surface can detach before its reference count reaches zero.
         * Keep the real NSView alive independently of the surface reference. */
        macdrv_retain_d3dmetal_content_view(view);
    }
    return owner;
}

void *macdrv_get_d3dmetal_view_owner(void *owner)
{
    return ((WineD3DMetalViewOwner *)owner)->context;
}

void macdrv_release_d3dmetal_view_owner(void *owner)
{
    [(WineD3DMetalViewOwner *)owner release];
}


@implementation WineMetalLayer

    /* Extend [CAMetalLayer nextDrawable] so client_surface_present() can be
     * called for the corresponding client_surface.
     */
    - (id<CAMetalDrawable>) nextDrawable
    {
        /* CAMetalLayer's delegate is the WineMetalView that contains it (seems like that's always true
         * for an NSView backing layer).
         * That WineMetalView's parent is the client_surface->cocoa_view WineContentView, it has the
         * client_surface set on it as an associated object.
         */
        if ([self.delegate isKindOfClass:NSClassFromString(@"WineMetalView")])
        {
            NSView* view = (NSView *)self.delegate;
            if ([view.superview isKindOfClass:NSClassFromString(@"WineContentView")] &&
                [view.window    isKindOfClass:NSClassFromString(@"WineWindow")])
            {
                void *client_surface = macdrv_get_view_d3dmetal_client_surface((WineContentView *)view.superview);
                if (client_surface)
                {
                    macdrv_event* event;
                    event = macdrv_create_event(CLIENT_SURFACE_PRESENTED, (WineWindow*)view.window);
                    macdrv_retain_d3dmetal_client_surface(client_surface);
                    event->client_surface_presented.client_surface = client_surface;

                    WineEventQueue *queue = [(WineWindow*)view.window queue];
                    [queue postEvent:event];
                    macdrv_release_event(event);
                }
            }
        }

        return [super nextDrawable];
    }

@end

#endif
