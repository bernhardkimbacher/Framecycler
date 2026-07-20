#include "metal_present_sync.h"

#import <AppKit/AppKit.h>
#import <QuartzCore/CAMetalLayer.h>

#include <QWindow>

namespace {

CAMetalLayer* find_metal_layer(NSView* view)
{
    if (!view) {
        return nil;
    }
    CALayer* layer = view.layer;
    if ([layer isKindOfClass:[CAMetalLayer class]]) {
        return (CAMetalLayer*)layer;
    }
    for (CALayer* sub in layer.sublayers) {
        if ([sub isKindOfClass:[CAMetalLayer class]]) {
            return (CAMetalLayer*)sub;
        }
    }
    return nil;
}

} // namespace

bool fc_metal_set_presents_with_transaction(QWindow* window, bool enabled)
{
    if (!window) {
        return false;
    }
    // Never force QWindow/NSWindow creation here — that must happen on the
    // GUI thread. Calling winId() without a platform handle creates the
    // native window on whichever thread we are on (crash on render thread).
    if (!window->handle()) {
        return false;
    }
    const WId wid = window->winId();
    if (!wid) {
        return false;
    }
    NSView* view = (__bridge NSView*)reinterpret_cast<void*>(wid);
    CAMetalLayer* metal = find_metal_layer(view);
    if (!metal) {
        return false;
    }
    metal.presentsWithTransaction = enabled ? YES : NO;
    return true;
}
