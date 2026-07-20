#pragma once

class QWindow;

#if defined(__APPLE__)

/// Toggle CAMetalLayer.presentsWithTransaction for a Qt Metal QWindow.
/// Returns false when no metal layer is found. Call from the render thread.
bool fc_metal_set_presents_with_transaction(QWindow* window, bool enabled);

#else

inline bool fc_metal_set_presents_with_transaction(QWindow*, bool)
{
    return false;
}

#endif
