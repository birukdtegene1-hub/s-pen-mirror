package ink.relay;

import android.content.Context;
import android.os.Build;
import android.view.MotionEvent;
import android.view.ViewConfiguration;
import java.nio.ByteBuffer;

/** Mouse gestures and monitor swipes from finger-only interactions on the preview. */
final class FingerTouchpad {
    interface Output { boolean send(byte[] event); }

    private enum PairGesture { UNDECIDED, SCROLL, PINCH }
    private static final double PINCH_STEP = Math.log(1.16);
    private final Output output;
    private final float density, slop, doubleTapSlop;
    private boolean active, moved, dragging, scrolling, dragCandidate, switchedMonitor;
    private int fingers, maxFingers;
    private long downAt, lastMoveAt, lastTapAt = -1;
    private float startX, startY, lastX, lastY, tapX, tapY;
    private float startSpan, zoomSpan;
    private PairGesture pairGesture;

    FingerTouchpad(Context context, Output output) {
        this.output = output;
        density = context.getResources().getDisplayMetrics().density;
        ViewConfiguration settings = ViewConfiguration.get(context);
        slop = settings.getScaledTouchSlop();
        doubleTapSlop = settings.getScaledDoubleTapSlop();
    }

    void accept(MotionEvent event, boolean monitorSwipeEnabled, boolean magnifierEnabled) {
        int action = event.getActionMasked();
        long time = event.getEventTime();
        boolean palmCanceled = Build.VERSION.SDK_INT >= 33
                && (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_POINTER_UP)
                && (event.getFlags() & MotionEvent.FLAG_CANCELED) != 0;
        if (action == MotionEvent.ACTION_CANCEL || palmCanceled) {
            cancel();
            return;
        }
        int count = 0;
        float x = 0, y = 0, span = 0;
        for (int i = 0; i < event.getPointerCount(); i++) {
            if (event.getToolType(i) != MotionEvent.TOOL_TYPE_FINGER) continue;
            if (action == MotionEvent.ACTION_POINTER_UP && i == event.getActionIndex()) continue;
            float pointerX = event.getX(i), pointerY = event.getY(i);
            if (count == 1) span = (float) Math.hypot(pointerX - x, pointerY - y);
            x += pointerX;
            y += pointerY;
            count++;
        }
        if (count > 0) { x /= count; y /= count; }

        if (action == MotionEvent.ACTION_DOWN) {
            cancelGesture();
            if (count == 0) return;
            active = true;
            downAt = time;
            maxFingers = 1;
            dragCandidate = lastTapAt >= 0 && time - lastTapAt <= ViewConfiguration.getDoubleTapTimeout()
                    && Math.hypot(x - tapX, y - tapY) <= doubleTapSlop;
            lastTapAt = -1;
            rebase(count, x, y, span, time);
        } else if (active && action == MotionEvent.ACTION_UP) {
            if (!moved && time - downAt <= ViewConfiguration.getLongPressTimeout() && maxFingers <= 3) {
                output.send(new byte[]{'C', (byte) maxFingers});
                if (maxFingers == 1 && !dragCandidate) {
                    lastTapAt = time;
                    tapX = x;
                    tapY = y;
                }
            }
            cancelGesture();
        } else if (active && (action == MotionEvent.ACTION_POINTER_DOWN || action == MotionEvent.ACTION_POINTER_UP
                || count != fingers)) {
            if (action == MotionEvent.ACTION_POINTER_UP && count == fingers - 1 && maxFingers == fingers) {
                int lifted = event.getActionIndex();
                float liftX = event.getX(lifted), liftY = event.getY(lifted);
                if (fingers == 3) {
                    swipe((2 * x + liftX) / 3, (2 * y + liftY) / 3, monitorSwipeEnabled);
                } else if (fingers == 2) {
                    movePair((x + liftX) / 2, (y + liftY) / 2,
                            (float) Math.hypot(x - liftX, y - liftY), magnifierEnabled);
                }
            }
            if (dragging) {
                output.send(new byte[]{'R'});
                dragging = false;
            }
            if (scrolling && count != 2) {
                output.send(new byte[]{'E'});
                scrolling = false;
            }
            maxFingers = Math.max(maxFingers, count);
            if (maxFingers > 1) dragCandidate = false;
            rebase(count, x, y, span, time);
        } else if (active && action == MotionEvent.ACTION_MOVE && count > 0) {
            if (count == 2 && maxFingers == 2) {
                movePair(x, y, span, magnifierEnabled);
                return;
            }
            if (count == 3 && maxFingers == 3) {
                swipe(x, y, monitorSwipeEnabled);
                return;
            }
            if (Math.hypot(x - startX, y - startY) <= slop && !moved) return;
            moved = true;
            float dx = (x - lastX) / density, dy = (y - lastY) / density;
            if (count == 1 && maxFingers == 1) {
                if (dragCandidate) {
                    dragging = output.send(new byte[]{'D'});
                    dragCandidate = false;
                }
                double speed = Math.hypot(dx, dy) / Math.max(1, time - lastMoveAt);
                float gain = (float) Math.min(1.8, 1 + speed * .28);
                sendDelta('M', dx * gain, dy * gain);
            }
            lastX = x;
            lastY = y;
            lastMoveAt = time;
        }
    }

    private void swipe(float x, float y, boolean enabled) {
        float horizontal = x - startX, vertical = y - startY;
        moved |= Math.hypot(horizontal, vertical) > slop;
        if (enabled && !switchedMonitor && Math.abs(horizontal) >= Math.max(36 * density, 3 * slop)
                && Math.abs(horizontal) >= 1.25f * Math.abs(vertical)) {
            switchedMonitor = output.send(new byte[]{'N', (byte) (horizontal < 0 ? 0 : 1)});
        }
    }

    private void movePair(float x, float y, float span, boolean magnifierEnabled) {
        double travel = Math.hypot(x - startX, y - startY);
        float spread = Math.abs(span - startSpan);
        if (pairGesture == PairGesture.UNDECIDED) {
            moved |= travel > slop || spread > slop;
            if (magnifierEnabled && startSpan >= 8 * density
                    && spread >= 8 * density && spread > travel * 1.4) {
                pairGesture = PairGesture.PINCH;
            } else if (travel > slop && (!magnifierEnabled || travel >= spread)) {
                pairGesture = PairGesture.SCROLL;
            }
        }
        if (pairGesture == PairGesture.PINCH) {
            double change = Math.log(Math.max(span, 4 * density) / zoomSpan);
            int steps = Math.max(-8, Math.min(8, (int) (change / PINCH_STEP)));
            if (steps != 0 && output.send(new byte[]{'Z', (byte) steps})) {
                zoomSpan *= (float) Math.exp(steps * PINCH_STEP);
            }
        } else if (pairGesture == PairGesture.SCROLL) {
            scrolling |= sendDelta('S', (lastX - x) / density, (lastY - y) / density);
            lastX = x;
            lastY = y;
        }
    }

    private void rebase(int count, float x, float y, float span, long time) {
        fingers = count;
        startX = lastX = x;
        startY = lastY = y;
        startSpan = zoomSpan = span;
        pairGesture = PairGesture.UNDECIDED;
        lastMoveAt = time;
    }

    private boolean sendDelta(char kind, float x, float y) {
        if (x == 0 && y == 0) return false;
        x = Math.max(-4096, Math.min(4096, x));
        y = Math.max(-4096, Math.min(4096, y));
        return output.send(ByteBuffer.allocate(9).put((byte) kind).putFloat(x).putFloat(y).array());
    }

    private void cancelGesture() {
        if (dragging) output.send(new byte[]{'R'});
        if (scrolling) output.send(new byte[]{'E'});
        dragging = scrolling = active = moved = switchedMonitor = false;
    }

    void cancel() {
        cancelGesture();
        lastTapAt = -1;
    }
}
