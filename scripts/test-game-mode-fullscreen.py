#!/usr/bin/env python3
"""Execute Wine's fullscreen methods with deterministic, display-free AppKit seams.

Run all cases, or pass --scenario monitor for the cross-monitor regression alone.
The backend is an NSObject, never an NSWindow: no application, Space, display,
run loop, or real screen is created or changed. Native callbacks are explicit.
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
    # Reflect all production ivars, including any added Game Mode state.
    ivars = header[opening + 1:block_end(header, opening) - 1]
    names = {
        "isGameModeFullscreenCandidate", "scheduleGameModeFullscreen",
        "reconcileGameModeFullscreen", "adjustFullScreenBehavior",
        "updateFullscreen", "setFrameAndWineFrame", "setFrameFromWine",
        "wine_fractionalFrame", "windowDidEnterFullScreen", "windowDidExitFullScreen",
        "windowDidFailToEnterFullScreen", "windowDidFailToExitFullScreen",
        "windowWillEnterFullScreen", "windowWillExitFullScreen", "windowDidResize",
    }
    methods = []
    declarations = []
    found = set()
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
    helper = source[start:block_end(source, opening)]
    return ivars, "\n".join(declarations), helper, "\n\n".join(methods)


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
+ (instancetype)sharedController;
- (void)flipRect:(NSRect *)rect;
- (void)updateFullscreenWindows;
- (void)adjustWindowLevels;
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
@end

@interface TestWindowBackend : NSObject
@property NSRect frame;
@property NSUInteger styleMask;
@property NSWindowCollectionBehavior collectionBehavior;
@property NSSize contentMinSize;
@property NSSize contentMaxSize;
@property NSUInteger toggles;
@property SEL scheduledSelector;
@property (retain) TestScreen *nativeScreen;
- (void)toggleFullScreen:(id)sender;
- (void)setFrame:(NSRect)frame display:(BOOL)display;
- (NSRect)frameRectForContentRect:(NSRect)frame;
- (NSRect)contentRectForFrameRect:(NSRect)frame;
- (id)screen;
- (id)parentWindow;
- (id)contentView;
- (NSArray *)childWineWindows;
- (BOOL)isVisible;
- (BOOL)isKeyWindow;
- (BOOL)isMiniaturized;
- (BOOL)isOrderedIn;
- (BOOL)isOnActiveSpace;
- (BOOL)inLiveResize;
- (void)runScheduled;
@end
@implementation TestWindowBackend
- (void)toggleFullScreen:(id)sender { self.toggles++; }
- (void)setFrame:(NSRect)frame display:(BOOL)display
{
    self.frame = frame;
    if (!(self.styleMask & NSWindowStyleMaskFullScreen))
        for (TestScreen *screen in screens)
            if (NSIntersectsRect(frame, screen.frame)) { self.nativeScreen = screen; break; }
}
- (NSRect)frameRectForContentRect:(NSRect)frame { return frame; }
- (NSRect)contentRectForFrameRect:(NSRect)frame { return frame; }
- (id)screen { return self.nativeScreen; }
- (id)parentWindow { return nil; }
- (id)contentView { return nil; }
- (NSArray *)childWineWindows { return @[]; }
- (BOOL)isVisible { return YES; }
- (BOOL)isKeyWindow { return YES; }
- (BOOL)isMiniaturized { return NO; }
- (BOOL)isOrderedIn { return YES; }
- (BOOL)isOnActiveSpace { return YES; }
- (BOOL)inLiveResize { return NO; }
- (void)performSelector:(SEL)selector withObject:(id)object afterDelay:(NSTimeInterval)delay
{
    require(!self.scheduledSelector || self.scheduledSelector == selector,
            "backend supports one coalesced scheduled selector");
    self.scheduledSelector = selector;
}
- (void)runScheduled
{
    SEL selector = self.scheduledSelector;
    self.scheduledSelector = NULL;
    if (selector) [self performSelector:selector];
}
@end

@interface WineWindow : TestWindowBackend
{
@public
__IVARS__
}
@property (readonly) NSRect wine_fractionalFrame;
@property (readonly) WineWindow *latentParentWindow;
- (BOOL)preventResizing;
- (void)adjustFeaturesForState;
- (void)doOrderOut;
- (void)postWindowFrameChanged:(NSRect)frame fullscreen:(BOOL)isFullscreen
                      resizing:(BOOL)resizing skipSizeMove:(BOOL)skipSizeMove;
__DECLARATIONS__
@end
static WineWindow *gameModeWindow;
__HELPER__
@implementation WineWindow
- (WineWindow *)latentParentWindow { return latentParentWindow; }
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

static NSRect A, B, C, ordinary;
static WineWindow *makeWindow(BOOL enabled)
{
    gameModeWindow = nil;
    WineWindow *window = [WineWindow new];
    window->gameModeHost = enabled;
    window->queue = [WineEventQueue new];
    window.collectionBehavior = NSWindowCollectionBehaviorParticipatesInCycle;
    [window setFrameAndWineFrame:ordinary];
    return window;
}
static void request(WineWindow *window, NSRect frame)
{
    [[WineApplicationController sharedController] flipRect:&frame];
    [window setFrameFromWine:frame];
}
static void finishEntry(WineWindow *window)
{
    window.styleMask |= NSWindowStyleMaskFullScreen;
    [window windowDidEnterFullScreen:nil];
    [window runScheduled];
}
static void enterA(WineWindow *window)
{
    request(window, A);
    [window runScheduled];
    require(window.toggles == 1, "fullscreen A must request native entry");
    [window windowWillEnterFullScreen:nil];
    finishEntry(window);
    requireFrame(window.frame, A, "initial native frame");
}
static void finishExit(WineWindow *window)
{
    window.styleMask &= ~NSWindowStyleMaskFullScreen;
    [window windowDidExitFullScreen:nil];
}
static void expectReentry(WineWindow *window, NSRect destination)
{
    requireFrame(window.frame, destination, "exit callback must apply latest destination");
    require(window->queue.frameEvents == 1, "exit must publish the applied frame");
    NSRect eventFrame = window->queue.deliveredFrame;
    [[WineApplicationController sharedController] flipRect:&eventFrame];
    requireFrame(eventFrame, destination, "Wine consumer must receive destination frame");
    [window runScheduled];
    require(window.toggles == 3, "destination must request native reentry");
    [window windowWillEnterFullScreen:nil];
    finishEntry(window);
    requireFrame(window.frame, destination, "reentry must preserve destination");
    requireFrame([[window screen] frame], destination, "native reentry must use destination screen");
    require(window.toggles == 3, "reentry must not immediately exit again");
}

int main(int argc, char **argv)
{
    @autoreleasepool
    {
        require(argc == 2, "one scenario is required");
        A = NSMakeRect(0, 0, 1920, 1080);
        B = NSMakeRect(1920, -360, 2560, 1440);
        C = NSMakeRect(-1600, 180, 1600, 900);
        ordinary = NSMakeRect(120, 160, 800, 600);
        NSMutableArray *values = [NSMutableArray array];
        for (NSValue *value in @[[NSValue valueWithRect:A], [NSValue valueWithRect:B], [NSValue valueWithRect:C]])
        {
            TestScreen *screen = [TestScreen new];
            screen.frame = value.rectValue;
            [values addObject:screen];
        }
        screens = values;
        BOOL off = !strcmp(argv[1], "off");
        WineWindow *window = makeWindow(!off);
        if (off)
        {
            request(window, B);
            [window runScheduled];
            requireFrame(window.frame, B, "OFF must apply screen-sized ordinary frame");
            request(window, ordinary);
            requireFrame(window.frame, ordinary, "OFF must apply windowed frame");
            require(window.toggles == 0, "OFF must not request native fullscreen");
        }
        else if (!strcmp(argv[1], "entry-windowed"))
        {
            request(window, A);
            [window runScheduled];
            require(window.toggles == 1, "entry must be requested");
            [window windowWillEnterFullScreen:nil];
            request(window, ordinary);
            [window runScheduled];
            requireFrame(window.frame, A, "entry must defer windowed geometry");
            require(window.toggles == 1, "entry must not overlap an exit");
            finishEntry(window);
            require(window.toggles == 2, "completed entry must request windowed exit");
            [window windowWillExitFullScreen:nil];
            finishExit(window);
            [window runScheduled];
            requireFrame(window.frame, ordinary, "windowed request during entry must survive");
            require(window.toggles == 2, "windowed request must not reenter");
        }
        else if (!strcmp(argv[1], "failed-entry") || !strcmp(argv[1], "failed-entry-same-screen") ||
                 !strcmp(argv[1], "failed-entry-windowed"))
        {
            request(window, A);
            [window runScheduled];
            [window windowWillEnterFullScreen:nil];
            NSRect destination = B;
            if (!strcmp(argv[1], "failed-entry-same-screen"))
            {
                destination = A;
                destination.size.width += 80;
            }
            if (!strcmp(argv[1], "failed-entry-windowed")) destination = ordinary;
            request(window, destination);
            [window windowDidFailToEnterFullScreen:nil];
            [window runScheduled];
            requireFrame(window.frame, destination, "failed entry must preserve the latest requested destination");
            require(window.toggles == 1, "failed entry must not immediately retry");
            require(!(window.styleMask & NSWindowStyleMaskFullScreen), "failed entry must remain windowed");
            if (!strcmp(argv[1], "failed-entry-windowed"))
            {
                request(window, A);
                [window runScheduled];
                require(window.toggles == 2, "restored windowed request must rearm a later fullscreen request");
            }
        }
        else
        {
            enterA(window);
            if (!strcmp(argv[1], "same-screen"))
            {
                // Distinct screen objects with identical frames are the same destination.
                TestScreen *equivalent = [TestScreen new];
                equivalent.frame = A;
                window.nativeScreen = equivalent;
                window->queue.frameEvents = 1;
                window->queue.unrelatedEvents = 1;
                request(window, A);
                require(window->queue.frameEvents == 0, "deferred reassert must discard stale frame events");
                require(window->queue.unrelatedEvents == 1, "discard must preserve unrelated events");
                [window runScheduled];
                require(window.toggles == 1, "same screen must not exit native fullscreen");
                requireFrame(window.frame, A, "same-screen request must preserve frame");
            }
            else if (!strcmp(argv[1], "user-exit"))
            {
                [window windowWillExitFullScreen:nil];
                finishExit(window);
                [window runScheduled];
                request(window, A);
                [window runScheduled];
                require(window.toggles == 1, "user exit must suppress automatic reentry");
                require(!(window.styleMask & NSWindowStyleMaskFullScreen), "user exit must remain windowed");
                request(window, ordinary);
                [window runScheduled];
                request(window, A);
                [window runScheduled];
                require(window.toggles == 2, "a genuine windowed request must rearm fullscreen");
            }
            else if (!strcmp(argv[1], "monitor") || !strcmp(argv[1], "latest") ||
                     !strcmp(argv[1], "failed-exit") || !strcmp(argv[1], "failed-exit-latest") ||
                     !strcmp(argv[1], "failed-exit-reassert") ||
                     !strcmp(argv[1], "windowed-exit") || !strcmp(argv[1], "failed-exit-windowed"))
            {
                BOOL windowed = !strcmp(argv[1], "windowed-exit") || !strcmp(argv[1], "failed-exit-windowed");
                request(window, windowed ? ordinary : B);
                [window runScheduled];
                require(window.toggles == 2, "new destination must request native exit");
                requireFrame(window.frame, A, "native exit must defer geometry until callback");
                [window windowWillExitFullScreen:nil];
                NSRect destination = windowed ? ordinary : B;
                if (!strcmp(argv[1], "latest") || !strcmp(argv[1], "failed-exit-latest") ||
                    !strcmp(argv[1], "failed-exit-reassert"))
                {
                    request(window, C);
                    [window runScheduled];
                    require(window.toggles == 2, "latest request must not overlap native exit");
                    destination = C;
                }
                if (!strcmp(argv[1], "failed-exit") || !strcmp(argv[1], "failed-exit-latest") ||
                    !strcmp(argv[1], "failed-exit-reassert") || !strcmp(argv[1], "failed-exit-windowed"))
                {
                    [window windowDidFailToExitFullScreen:nil];
                    [window runScheduled];
                    [window windowDidResize:nil];
                    [window runScheduled];
                    require(window.toggles == 2, "failed exit must not immediately retry on resize");
                    requireFrame(window.frame, A, "failed exit must retain native geometry");
                    if (!strcmp(argv[1], "failed-exit-reassert"))
                    {
                        request(window, A);
                        [window runScheduled];
                        destination = A;
                    }
                    if (strcmp(argv[1], "failed-exit"))
                    {
                        [window windowWillExitFullScreen:nil];
                        finishExit(window);
                        [window runScheduled];
                        requireFrame(window.frame, destination, "later user exit must restore the latest destination");
                        require(window.toggles == 2, "later user exit must not automatically reenter");
                        if (windowed)
                        {
                            request(window, A);
                            [window runScheduled];
                            require(window.toggles == 3, "restored windowed exit must allow a later fullscreen request");
                        }
                    }
                }
                else
                {
                    finishExit(window);
                    if (windowed)
                    {
                        [window runScheduled];
                        requireFrame(window.frame, ordinary, "ordinary exit must apply windowed frame");
                        require(window.toggles == 2, "ordinary windowed exit must not reenter");
                    }
                    else expectReentry(window, destination);
                }
            }
            else require(NO, "unknown scenario");
        }
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
                        "-Werror", "-framework", "AppKit",
                        "-framework", "QuartzCore", str(driver_source), "-o", str(cls.driver)],
                       check=True)

    def run_scenario(self, scenario):
        subprocess.run([str(self.driver), scenario], check=True)

    def test_monitor(self):
        self.run_scenario("monitor")

    def test_latest(self):
        self.run_scenario("latest")

    def test_entry_windowed(self):
        self.run_scenario("entry-windowed")

    def test_same_screen(self):
        self.run_scenario("same-screen")

    def test_user_exit(self):
        self.run_scenario("user-exit")

    def test_off(self):
        self.run_scenario("off")

    def test_failed_exit(self):
        self.run_scenario("failed-exit")

    def test_failed_entry(self):
        self.run_scenario("failed-entry")

    def test_failed_entry_same_screen(self):
        self.run_scenario("failed-entry-same-screen")

    def test_failed_entry_windowed(self):
        self.run_scenario("failed-entry-windowed")

    def test_failed_exit_latest(self):
        self.run_scenario("failed-exit-latest")

    def test_failed_exit_reassert(self):
        self.run_scenario("failed-exit-reassert")

    def test_failed_exit_windowed(self):
        self.run_scenario("failed-exit-windowed")

    def test_windowed_exit(self):
        self.run_scenario("windowed-exit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["monitor", "latest", "entry-windowed", "same-screen",
                                             "user-exit", "off", "failed-exit", "failed-entry", "failed-entry-same-screen",
                                             "failed-entry-windowed",
                                             "failed-exit-latest", "failed-exit-reassert", "failed-exit-windowed", "windowed-exit"])
    options, unittest_args = parser.parse_known_args()
    arguments = [__file__, *unittest_args]
    if options.scenario:
        arguments.append("GameModeFullscreenTests.test_" + options.scenario.replace("-", "_"))
    unittest.main(argv=arguments)
