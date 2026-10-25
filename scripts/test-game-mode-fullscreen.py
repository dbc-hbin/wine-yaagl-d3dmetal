#!/usr/bin/env python3
"""Exercise Wine window geometry without automatic native-fullscreen transitions.

The backend is an NSObject, never an NSWindow. No application, Space, display,
GameMode policy, run loop, or real screen is changed; native callbacks are explicit.
"""

from pathlib import Path
import argparse
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def block_end(source, opening):
    """Find a balanced C/Objective-C block, ignoring comments and literals."""
    tokens = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', re.S)
    depth = 0
    for match in tokens.finditer(source, opening):
        if match.group() == "{":
            depth += 1
        elif match.group() == "}":
            depth -= 1
            if depth == 0:
                return match.end()
    raise ValueError("Unterminated production block")


def production_fragments():
    directory = ROOT / "dlls/winemac.drv"
    source = (directory / "cocoa_window.m").read_text()
    header = (directory / "cocoa_window.h").read_text()
    opening = header.index("{", header.index("@interface WineWindow"))
    ivars = header[opening + 1:block_end(header, opening) - 1]
    names = {
        "adjustFullScreenBehavior", "updateFullscreen", "setFrameAndWineFrame",
        "setFrameFromWine", "wine_fractionalFrame", "windowDidEnterFullScreen",
        "windowDidExitFullScreen", "windowDidFailToEnterFullScreen",
        "windowDidFailToExitFullScreen", "windowWillEnterFullScreen",
        "windowWillExitFullScreen", "windowDidResize", "windowDidBecomeKey",
        "toggleFullScreen",
    }
    methods, declarations, found = [], [], set()
    for match in re.finditer(r"^[ \t]*-\s*\([^\n)]+\)\s*(\w+)", source, re.M):
        if match[1] not in names:
            continue
        opening = source.index("{", match.end())
        end = block_end(source, opening)
        methods.append(source[match.start():end])
        declarations.append(source[match.start():opening].strip() + ";")
        found.add(match[1])
    if found != names:
        raise ValueError(f"Missing production methods: {sorted(names - found)}")
    start = source.index("static NSScreen* screen_covered_by_rect(")
    opening = source.index("{", start)
    return ivars, "\n".join(declarations), source[start:block_end(source, opening)], "\n\n".join(methods)


BACKEND = r'''
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#include <float.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef TRUE
#define TRUE YES
#endif
#ifndef FALSE
#define FALSE NO
#endif
#define WINDOW_FRAME_CHANGED 1
#define event_mask_for_type(type) (1u << (type))

static void require(BOOL value, const char *message)
{
    if (!value) { fprintf(stderr, "%s\n", message); exit(1); }
}
static void requireFrame(NSRect actual, NSRect expected, const char *message)
{
    if (!NSEqualRects(actual, expected))
    {
        fprintf(stderr, "%s: got %s, expected %s\n", message,
                [NSStringFromRect(actual) UTF8String], [NSStringFromRect(expected) UTF8String]);
        exit(1);
    }
}

@interface TestScreen : NSObject
@property NSRect frame;
+ (NSArray *)screens;
@end
static NSArray *screens;
@implementation TestScreen
+ (NSArray *)screens { return screens; }
@end
#define NSScreen TestScreen

@class WineWindow;
static WineWindow *causing_becomeKeyWindow;
@interface WineEventQueue : NSObject
@property NSUInteger frameEvents;
@property NSUInteger unrelatedEvents;
@property NSRect deliveredFrame;
@property BOOL deliveredFullscreen;
- (void)discardEventsMatchingMask:(NSUInteger)mask forWindow:(id)window;
@end
@implementation WineEventQueue
- (void)discardEventsMatchingMask:(NSUInteger)mask forWindow:(id)window
{
    if (mask & event_mask_for_type(WINDOW_FRAME_CHANGED)) self.frameEvents = 0;
}
@end

@interface WineApplicationController : NSObject
@property NSUInteger focusEvents;
+ (instancetype)sharedController;
- (void)flipRect:(NSRect *)rect;
- (void)updateFullscreenWindows;
- (void)adjustWindowLevels;
- (NSEvent *)lastFlagsChanged;
- (void)windowGotFocus:(WineWindow *)window;
@end
@implementation WineApplicationController
+ (instancetype)sharedController
{
    static WineApplicationController *controller;
    if (!controller) controller = [self new];
    return controller;
}
- (void)flipRect:(NSRect *)rect { rect->origin.y = 1080 - NSMaxY(*rect); }
- (void)updateFullscreenWindows {}
- (void)adjustWindowLevels {}
- (NSEvent *)lastFlagsChanged { return nil; }
- (void)windowGotFocus:(WineWindow *)window { self.focusEvents++; }
@end

@interface TestWindowBackend : NSObject
@property NSRect frame;
@property NSUInteger styleMask;
@property NSWindowCollectionBehavior collectionBehavior;
@property NSSize contentMinSize;
@property NSSize contentMaxSize;
@property NSUInteger toggles;
- (void)toggleFullScreen:(id)sender;
- (void)setFrame:(NSRect)frame display:(BOOL)display;
- (NSRect)frameRectForContentRect:(NSRect)frame;
- (NSRect)contentRectForFrameRect:(NSRect)frame;
- (id)parentWindow;
- (id)contentView;
- (NSArray *)childWineWindows;
- (BOOL)isVisible;
- (BOOL)isKeyWindow;
- (BOOL)isOrderedIn;
- (BOOL)isOnActiveSpace;
- (BOOL)inLiveResize;
- (void)flagsChanged:(NSEvent *)event;
@end
@implementation TestWindowBackend
- (void)toggleFullScreen:(id)sender { self.toggles++; }
- (void)setFrame:(NSRect)frame display:(BOOL)display { self.frame = frame; }
- (NSRect)frameRectForContentRect:(NSRect)frame { return frame; }
- (NSRect)contentRectForFrameRect:(NSRect)frame { return frame; }
- (id)parentWindow { return nil; }
- (id)contentView { return nil; }
- (NSArray *)childWineWindows { return @[]; }
- (BOOL)isVisible { return YES; }
- (BOOL)isKeyWindow { return YES; }
- (BOOL)isOrderedIn { return YES; }
- (BOOL)isOnActiveSpace { return YES; }
- (BOOL)inLiveResize { return NO; }
- (void)flagsChanged:(NSEvent *)event {}
@end

@interface WineWindow : TestWindowBackend
{
@public
__IVARS__
}
@property (readonly) NSRect wine_fractionalFrame;
@property (readonly) WineWindow *latentParentWindow;
@property (readonly) BOOL disabled;
- (BOOL)preventResizing;
- (void)adjustFeaturesForState;
- (void)doOrderOut;
- (void)postWindowFrameChanged:(NSRect)frame fullscreen:(BOOL)isFullscreen
                      resizing:(BOOL)resizing skipSizeMove:(BOOL)skipSizeMove;
__DECLARATIONS__
@end
__HELPER__
@implementation WineWindow
- (WineWindow *)latentParentWindow { return latentParentWindow; }
- (BOOL)disabled { return disabled; }
- (BOOL)preventResizing { return NO; }
- (void)adjustFeaturesForState {}
- (void)doOrderOut { require(NO, "unexpected order-out in visible-window scenario"); }
- (void)postWindowFrameChanged:(NSRect)frame fullscreen:(BOOL)isFullscreen
                      resizing:(BOOL)resizing skipSizeMove:(BOOL)skipSizeMove
{
    [queue discardEventsMatchingMask:event_mask_for_type(WINDOW_FRAME_CHANGED) forWindow:self];
    [[WineApplicationController sharedController] flipRect:&frame];
    queue.deliveredFrame = frame;
    queue.deliveredFullscreen = isFullscreen;
    queue.frameEvents++;
}
__METHODS__
@end

static NSRect A, B, ordinary;
static WineWindow *makeWindow(void)
{
    WineWindow *window = [WineWindow new];
    window->queue = [WineEventQueue new];
    window->resizable = TRUE;
    window.styleMask = NSWindowStyleMaskResizable;
    window.collectionBehavior = NSWindowCollectionBehaviorParticipatesInCycle;
    [window setFrameAndWineFrame:ordinary];
    [window adjustFullScreenBehavior:window.collectionBehavior];
    return window;
}
static void request(WineWindow *window, NSRect frame)
{
    [[WineApplicationController sharedController] flipRect:&frame];
    [window setFrameFromWine:frame];
}
static void expectEvent(WineWindow *window, NSRect frame, BOOL nativeFullscreen)
{
    require(window->queue.frameEvents == 1, "one authoritative frame event must reach Wine");
    NSRect actual = window->queue.deliveredFrame;
    [[WineApplicationController sharedController] flipRect:&actual];
    requireFrame(actual, frame, "Wine must receive the actual frame geometry");
    require(window->queue.deliveredFullscreen == nativeFullscreen, "event must distinguish native and borderless fullscreen");
}
static void beginManualEntry(WineWindow *window)
{
    [window toggleFullScreen:nil];
    require(window.toggles == 1, "only the explicit user request may enter native fullscreen");
    [window windowWillEnterFullScreen:nil];
}
static void finishManualEntry(WineWindow *window, NSRect native)
{
    window.frame = native;
    window.styleMask |= NSWindowStyleMaskFullScreen;
    [window windowDidEnterFullScreen:nil];
    expectEvent(window, native, YES);
}
static void finishManualExit(WineWindow *window)
{
    window.styleMask &= ~NSWindowStyleMaskFullScreen;
    [window windowDidExitFullScreen:nil];
}

int main(int argc, char **argv)
{
    @autoreleasepool
    {
        require(argc == 2, "one scenario is required");
        A = NSMakeRect(0, 0, 1920, 1080);
        B = NSMakeRect(1920, -360, 2560, 1440);
        ordinary = NSMakeRect(120, 160, 800, 600);
        NSMutableArray *values = [NSMutableArray array];
        for (NSValue *value in @[[NSValue valueWithRect:A], [NSValue valueWithRect:B]])
        {
            TestScreen *screen = [TestScreen new];
            screen.frame = value.rectValue;
            [values addObject:screen];
        }
        screens = values;
        WineWindow *window = makeWindow();
        if (!strcmp(argv[1], "windowed") || !strcmp(argv[1], "monitor"))
        {
            NSArray *requests = !strcmp(argv[1], "monitor")
                ? @[[NSValue valueWithRect:A], [NSValue valueWithRect:B], [NSValue valueWithRect:ordinary]]
                : @[[NSValue valueWithRect:NSMakeRect(2200, 100, 900, 700)], [NSValue valueWithRect:ordinary]];
            window->queue.unrelatedEvents = 1;
            for (NSValue *value in requests)
            {
                request(window, value.rectValue);
                [window windowDidBecomeKey:nil];
                requireFrame(window.frame, value.rectValue, "Wine requests must apply without a native Space transition");
                require(window.toggles == 0, "resize and focus must not force native fullscreen");
                require(!(window.styleMask & NSWindowStyleMaskFullScreen), "borderless and windowed requests must stay outside native fullscreen");
                require(window->fullscreen == (screen_covered_by_rect(value.rectValue, screens) != nil), "borderless coverage must retain Wine fullscreen bookkeeping");
                expectEvent(window, value.rectValue, NO);
                require(window->queue.unrelatedEvents == 1, "frame coalescing must preserve unrelated events");
            }
            require([WineApplicationController sharedController].focusEvents == requests.count, "normal focus delivery must remain intact");
        }
        else if (!strcmp(argv[1], "inset"))
        {
            beginManualEntry(window);
            NSRect native = A;
            native.size.height -= 31;
            finishManualEntry(window, native);
            request(window, native);
            expectEvent(window, native, YES);
            requireFrame(window->nonFullscreenFrame, ordinary, "native geometry acknowledgments must preserve the restore frame");
            window->queue.frameEvents = 0;
            request(window, native);
            require(window->queue.frameEvents == 0, "acknowledgment must not cause a correction loop");
            request(window, A);
            requireFrame(window.frame, native, "screen-sized reassertions must not overwrite inset native geometry");
            expectEvent(window, native, YES);
            request(window, ordinary);
            require(window.toggles == 1, "a Wine request must not force a native exit");
            [window toggleFullScreen:nil];
            [window windowWillExitFullScreen:nil];
            finishManualExit(window);
            require(window.toggles == 2, "manual exit must not reenter native fullscreen");
            requireFrame(window.frame, ordinary, "manual exit must restore the latest Wine request");
            expectEvent(window, ordinary, NO);
        }
        else if (!strcmp(argv[1], "user-exit"))
        {
            request(window, A);
            beginManualEntry(window);
            finishManualEntry(window, A);
            [window toggleFullScreen:nil];
            [window windowWillExitFullScreen:nil];
            finishManualExit(window);
            [window windowDidBecomeKey:nil];
            request(window, A);
            requireFrame(window.frame, A, "explicit native exit must retain borderless geometry");
            require(window.toggles == 2, "screen coverage and focus must not undo a user exit");
            require(!(window.styleMask & NSWindowStyleMaskFullScreen), "user exit must remain outside native fullscreen");
        }
        else if (!strcmp(argv[1], "latest") || !strcmp(argv[1], "failed-entry") || !strcmp(argv[1], "failed-exit"))
        {
            beginManualEntry(window);
            request(window, A);
            request(window, B);
            requireFrame(window.frame, ordinary, "native entry must defer Wine geometry changes");
            if (!strcmp(argv[1], "failed-entry"))
            {
                [window windowDidFailToEnterFullScreen:nil];
                requireFrame(window.frame, B, "failed entry must apply the latest deferred request");
                require(window.toggles == 1, "failed entry must not automatically retry");
                expectEvent(window, B, NO);
            }
            else
            {
                finishManualEntry(window, A);
                requireFrame(window->nonFullscreenFrame, B, "entry must preserve the pending destination");
                [window toggleFullScreen:nil];
                [window windowWillExitFullScreen:nil];
                if (!strcmp(argv[1], "failed-exit"))
                {
                    [window windowDidFailToExitFullScreen:nil];
                    requireFrame(window.frame, A, "failed exit must retain native geometry");
                    expectEvent(window, A, YES);
                    [window windowDidBecomeKey:nil];
                    require(window.toggles == 2, "failed exit must not automatically retry");
                    [window toggleFullScreen:nil];
                    [window windowWillExitFullScreen:nil];
                }
                finishManualExit(window);
                requireFrame(window.frame, B, "manual exit must apply the pending monitor geometry");
                expectEvent(window, B, NO);
                require(window.toggles == (!strcmp(argv[1], "failed-exit") ? 3 : 2), "restored monitor must not force native reentry");
            }
        }
        else require(NO, "unknown scenario");
    }
    return 0;
}
'''


class GameModeFullscreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="game-mode-fullscreen-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        ivars, declarations, helper, methods = production_fragments()
        source = BACKEND.replace("__IVARS__", ivars).replace("__DECLARATIONS__", declarations)
        source = source.replace("__HELPER__", helper).replace("__METHODS__", methods)
        directory = Path(cls.temporary.name)
        driver_source = directory / "driver.m"
        driver_source.write_text(source)
        cls.driver = directory / "driver"
        subprocess.run(["clang", "-x", "objective-c", "-fno-objc-arc", "-Wall",
                        "-Werror", "-framework", "AppKit", "-framework", "QuartzCore",
                        str(driver_source), "-o", str(cls.driver)], check=True)

    def run_scenario(self, scenario):
        subprocess.run([str(self.driver), scenario], check=True)

    def test_windowed(self):
        self.run_scenario("windowed")

    def test_monitor(self):
        self.run_scenario("monitor")

    def test_inset(self):
        self.run_scenario("inset")

    def test_user_exit(self):
        self.run_scenario("user-exit")

    def test_latest(self):
        self.run_scenario("latest")

    def test_failed_entry(self):
        self.run_scenario("failed-entry")

    def test_failed_exit(self):
        self.run_scenario("failed-exit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["windowed", "monitor", "inset", "user-exit",
                                             "latest", "failed-entry", "failed-exit"])
    options, unittest_args = parser.parse_known_args()
    arguments = [__file__, *unittest_args]
    if options.scenario:
        arguments.append("GameModeFullscreenTests.test_" + options.scenario.replace("-", "_"))
    unittest.main(argv=arguments)
