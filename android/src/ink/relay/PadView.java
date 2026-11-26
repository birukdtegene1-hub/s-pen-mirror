package ink.relay;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Rect;
import android.os.Build;
import android.view.MotionEvent;
import android.view.View;
import java.nio.ByteBuffer;

final class PadView extends View {
    private static final int STYLUS_BUTTONS = MotionEvent.BUTTON_STYLUS_PRIMARY | MotionEvent.BUTTON_STYLUS_SECONDARY;
    private final RelayApp app;
    private final Runnable historyChanged;
    private final CastViewport viewport;
    private final FingerTouchpad touchpad;
    private final Paint previewPaint = new Paint(Paint.FILTER_BITMAP_FLAG);
    private final Paint cursorPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private Link.CastFrame preview = Link.CastFrame.EMPTY;
    InkPage page;
    private final Rect dirty = new Rect();
    private boolean full = true, scheduled;
    private boolean remoteStroke;
    private boolean castStroke, suppressNavigation;
    private boolean panWithFingers;
    private boolean sideButtonDown, hardwareEraser, strokeEraser, segmentOpen, hovering;
    private float stylusX, stylusY;
    private int penId = -1;
    private int strokeRevision, firstFinger = -1, secondFinger = -1;
    private float touchX, touchY, touchSpan;
    private float scale, left, top;

    PadView(Context ctx, CastViewport viewport, Runnable historyChanged) {
        super(ctx);
        app = (RelayApp) ctx.getApplicationContext();
        this.historyChanged = historyChanged;
        this.viewport = viewport;
        touchpad = new FingerTouchpad(ctx, app.link::offerPointer);
        cursorPaint.setStyle(Paint.Style.STROKE);
        setContentDescription("S-Pen writing area");
        setFocusable(true);
    }

    @Override protected void onSizeChanged(int w, int h, int oldW, int oldH) {
        if (w <= 0 || h <= 0) return;
        cancelStroke();
        page = app.page(w, h);
        fitPage(w, h);
        layoutPreview();
        post(historyChanged);
    }

    void rotatePage() {
        if (page == null) return;
        cancelStroke();
        page.rotateClockwise();
        fitPage(getWidth(), getHeight());
    }

    private void fitPage(int w, int h) {
        scale = Math.min((float) w / page.width, (float) h / page.height);
        left = (w - page.width * scale) / 2;
        top = (h - page.height * scale) / 2;
        syncPage();
    }

    @Override protected void onDraw(Canvas canvas) {
        drawContent(canvas);
        if (hovering && page != null && erasing()) {
            float radius = page.eraserSize * 3.5f / 2;
            if (app.link.casting() && preview.bitmap != null) radius *= viewport.image.width() / preview.bitmap.getWidth();
            else if (!app.link.writing) radius *= scale;
            radius = Math.max(Ui.dp(getContext(), 4), radius);
            cursorPaint.setColor(0xff172a20);
            cursorPaint.setStrokeWidth(Ui.dp(getContext(), 3));
            canvas.drawCircle(stylusX, stylusY, radius, cursorPaint);
            cursorPaint.setColor(0xfff8fbf9);
            cursorPaint.setStrokeWidth(Ui.dp(getContext(), 1));
            canvas.drawCircle(stylusX, stylusY, radius, cursorPaint);
        }
    }

    private void drawContent(Canvas canvas) {
        canvas.drawColor(0xffdce1d8);
        if (app.link.casting()) {
            canvas.drawColor(0xff202725);
            if (preview.bitmap != null) canvas.drawBitmap(preview.bitmap, null, viewport.image, previewPaint);
            return;
        }
        if (page == null) return;
        if (app.link.writing) {
            canvas.drawColor(0xfffffefa);
            return;
        }
        canvas.save();
        canvas.translate(left, top);
        canvas.scale(scale, scale);
        canvas.clipRect(0, 0, page.width, page.height);
        page.draw(canvas);
        canvas.restore();
    }

    boolean erasing() { return app.eraserActive || hardwareEraser; }

    void selectEraser(boolean enabled) {
        app.eraserActive = enabled;
        historyChanged.run();
        postInvalidateOnAnimation();
    }

    void clearStylus() {
        sideButtonDown = false;
        hideStylus();
    }

    void cancelFingerInput() {
        touchpad.cancel();
        resetNavigation();
    }

    void setPanWithFingers(boolean enabled) {
        if (panWithFingers != enabled) cancelFingerInput();
        panWithFingers = enabled;
    }

    private void hideStylus() {
        if (!hovering && !hardwareEraser) return;
        boolean changed = hardwareEraser;
        hovering = false;
        hardwareEraser = false;
        if (changed) historyChanged.run();
        postInvalidateOnAnimation();
    }

    private static boolean isStylus(int toolType) {
        return toolType == MotionEvent.TOOL_TYPE_STYLUS || toolType == MotionEvent.TOOL_TYPE_ERASER;
    }

    private void trackStylus(MotionEvent event, int pointer, int buttons) {
        if (!isStylus(event.getToolType(pointer))) return;
        stylusX = event.getX(pointer);
        stylusY = event.getY(pointer);
        hovering = true;
        boolean previous = erasing();
        boolean pressed = (buttons & STYLUS_BUTTONS) != 0;
        hardwareEraser = event.getToolType(pointer) == MotionEvent.TOOL_TYPE_ERASER;
        if (pressed && !sideButtonDown) app.eraserActive = !app.eraserActive;
        sideButtonDown = pressed;
        if (previous != erasing()) historyChanged.run();
        if (previous || erasing()) postInvalidateOnAnimation();
    }

    @Override public boolean onHoverEvent(MotionEvent event) {
        if (!isStylus(event.getToolType(0))) return super.onHoverEvent(event);
        if (event.getActionMasked() == MotionEvent.ACTION_HOVER_EXIT) {
            if (penId == -1) hideStylus();
        } else trackStylus(event, 0, event.getButtonState());
        return true;
    }

    @Override public boolean onGenericMotionEvent(MotionEvent event) {
        int action = event.getActionMasked();
        if (isStylus(event.getToolType(0))
                && (action == MotionEvent.ACTION_BUTTON_PRESS || action == MotionEvent.ACTION_BUTTON_RELEASE)) {
            int button = event.getActionButton() & STYLUS_BUTTONS;
            if (button != 0) {
                int state = event.getButtonState();
                trackStylus(event, 0, action == MotionEvent.ACTION_BUTTON_PRESS ? state | button : state & ~button);
                return true;
            }
        }
        return super.onGenericMotionEvent(event);
    }

    void syncPage() {
        full = true;
        update();
    }

    void showPreview(Link.CastFrame frame) {
        if (preview == frame) return;
        if (preview.touchpad != frame.touchpad) cancelFingerInput();
        if (preview.revision != frame.revision || frame.status != Link.CastFrame.READY) {
            if (penId != -1 && remoteStroke) {
                finishSegment();
                end();
            }
            resetNavigation();
            if (frame.status == Link.CastFrame.SELECT) viewport.fit();
        }
        boolean resize = frame.bitmap != null && (preview.bitmap == null
                || preview.bitmap.getWidth() != frame.bitmap.getWidth()
                || preview.bitmap.getHeight() != frame.bitmap.getHeight());
        preview = frame;
        if (resize) layoutPreview();
        postInvalidateOnAnimation();
    }

    private void layoutPreview() {
        if (preview.bitmap != null && getWidth() > 0 && getHeight() > 0) {
            viewport.layout(getWidth(), getHeight(), preview.bitmap.getWidth(), preview.bitmap.getHeight());
        }
    }

    void adjustPreview(Runnable adjustment) {
        if (penId != -1 || preview.bitmap == null) return;
        adjustment.run();
        postInvalidateOnAnimation();
    }

    void editDesktop(byte action) {
        cancelStroke();
        app.link.offerAnnotation(new byte[]{action}, preview.revision);
    }

    private void update() {
        postInvalidateOnAnimation();
        if (!scheduled && isAttachedToWindow() && app.link.state == Link.State.ONLINE
                && !app.link.screen && !app.link.writing) {
            scheduled = true;
            postOnAnimation(pump);
        }
    }

    private final Runnable pump = new Runnable() {
        @Override public void run() {
            scheduled = false;
            if (page == null || !isShown() || app.link.screen || app.link.writing
                    || app.link.state != Link.State.ONLINE) return;
            if (full) dirty.set(0, 0, page.width, page.height);
            if (dirty.isEmpty() || !dirty.intersect(0, 0, page.width, page.height)) return;
            if (!app.link.ready(false)) {
                scheduled = true;
                postDelayed(this, 20);
                return;
            }
            Bitmap area = InkPage.bitmap(dirty.width(), dirty.height());
            Canvas canvas = new Canvas(area);
            canvas.translate(-dirty.left, -dirty.top);
            page.draw(canvas);
            if (app.link.offer(full ? 'I' : 'P', area, dirty.left, dirty.top)) {
                full = false;
                dirty.setEmpty();
            } else update();
        }
    };

    @Override public boolean onTouchEvent(MotionEvent event) {
        if (page == null) return true;
        int action = event.getActionMasked(), actionIndex = event.getActionIndex();
        boolean casting = app.link.casting();
        if (action == MotionEvent.ACTION_DOWN) suppressNavigation = false;
        boolean isEdge = action == MotionEvent.ACTION_DOWN || action == MotionEvent.ACTION_POINTER_DOWN
                || action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_POINTER_UP;
        boolean useTouchpad = casting && preview.touchpad && !panWithFingers;
        if (useTouchpad && penId == -1 && !suppressNavigation
                && (!isEdge || event.getToolType(actionIndex) == MotionEvent.TOOL_TYPE_FINGER)) {
            touchpad.accept(event, app.link.monitorSwipeSupported, app.link.magnifierSupported);
            return true;
        }
        if (action == MotionEvent.ACTION_DOWN || action == MotionEvent.ACTION_POINTER_DOWN) {
            int type = event.getToolType(actionIndex);
            boolean stylus = isStylus(type);
            boolean writing = app.link.writing;
            if (penId != -1) return true;
            if (casting && !stylus) {
                if (!useTouchpad && !suppressNavigation) navigate(event);
                return true;
            }
            if (writing ? !stylus : !stylus && !page.fingers) return true;
            if (stylus) trackStylus(event, actionIndex, event.getButtonState());
            float x = writing ? event.getX(actionIndex) : (event.getX(actionIndex) - left) / scale;
            float y = writing ? event.getY(actionIndex) : (event.getY(actionIndex) - top) / scale;
            if (writing ? (x < 0 || x >= getWidth() || y < 0 || y >= getHeight())
                    : (x < 0 || x >= page.width || y < 0 || y >= page.height)) return true;
            if (casting && (preview.status != Link.CastFrame.READY || preview.bitmap == null
                    || !viewport.image.contains(x, y))) return true;
            strokeRevision = preview.revision;
            remoteStroke = writing;
            castStroke = casting;
            if (!beginSegment(event, actionIndex, erasing())) {
                remoteStroke = castStroke = false;
                return true;
            }
            penId = event.getPointerId(actionIndex);
            if (casting) {
                suppressNavigation = true;
                cancelFingerInput();
            }
            getParent().requestDisallowInterceptTouchEvent(true);
        } else if (action == MotionEvent.ACTION_MOVE && penId != -1) {
            int idx = event.findPointerIndex(penId);
            if (idx != -1) {
                boolean stylus = isStylus(event.getToolType(idx));
                if (stylus) trackStylus(event, idx, event.getButtonState());
                boolean erasing = erasing();
                if (segmentOpen) {
                    for (int i = 0; i < event.getHistorySize(); i++) add(event, idx, i);
                }
                if (!segmentOpen || erasing != strokeEraser) {
                    finishSegment();
                    beginSegment(event, idx, erasing);
                } else add(event, idx, -1);
            }
        } else if ((action == MotionEvent.ACTION_POINTER_UP || action == MotionEvent.ACTION_UP)
                && penId == event.getPointerId(actionIndex)) {
            if (Build.VERSION.SDK_INT >= 33 && (event.getFlags() & MotionEvent.FLAG_CANCELED) != 0) cancelStroke();
            else {
                if (segmentOpen) add(event, actionIndex, -1);
                finishSegment();
                end();
            }
            trackStylus(event, actionIndex, event.getButtonState());
        } else if (action == MotionEvent.ACTION_CANCEL) {
            cancelStroke();
            clearStylus();
        }
        if (casting && !useTouchpad && penId == -1 && !suppressNavigation) navigate(event);
        update();
        return true;
    }

    private void add(MotionEvent e, int idx, int history) {
        float x = history < 0 ? e.getX(idx) : e.getHistoricalX(idx, history);
        float y = history < 0 ? e.getY(idx) : e.getHistoricalY(idx, history);
        float p = history < 0 ? e.getPressure(idx) : e.getHistoricalPressure(idx, history);
        if (remoteStroke) sendRemotePoint(x, y, p);
        else page.point((x - left) / scale, (y - top) / scale, p, dirty);
    }

    private boolean beginSegment(MotionEvent event, int index, boolean erasing) {
        strokeEraser = erasing;
        if (remoteStroke) segmentOpen = sendStart(event, index, erasing);
        else {
            page.begin(erasing);
            segmentOpen = true;
            add(event, index, -1);
        }
        return segmentOpen;
    }

    private void finishSegment() {
        if (!segmentOpen) return;
        if (remoteStroke) sendRemoteEnd();
        else page.finish(dirty);
        segmentOpen = false;
    }

    private boolean sendStart(MotionEvent event, int index, boolean erasing) {
        InkPage.Tool selected = erasing ? page.eraserTool() : page.tool;
        byte[] data = ByteBuffer.allocate(30).put((byte) 'S')
                .putFloat(normalizeX(event.getX(index), castStroke)).putFloat(normalizeY(event.getY(index), castStroke))
                .putFloat(pressure(event.getPressure(index))).put((byte) selected.ordinal()).putInt(page.color)
                .putFloat(erasing ? page.eraserSize : page.size).putInt(Math.min(10000, getWidth()))
                .putInt(Math.min(10000, getHeight())).array();
        return app.link.offerAnnotation(data, strokeRevision);
    }

    private void sendRemotePoint(float x, float y, float pressure) {
        byte[] data = ByteBuffer.allocate(13).put((byte) 'M')
                .putFloat(normalizeX(x, castStroke)).putFloat(normalizeY(y, castStroke))
                .putFloat(pressure(pressure)).array();
        app.link.offerAnnotation(data, strokeRevision);
    }

    private void sendRemoteEnd() {
        app.link.offerAnnotation(new byte[]{'E'}, strokeRevision);
    }

    private float normalizeX(float x, boolean casting) {
        return casting ? viewport.imageX(x) : Math.max(0, Math.min(1, x / Math.max(1, getWidth())));
    }

    private float normalizeY(float y, boolean casting) {
        return casting ? viewport.imageY(y) : Math.max(0, Math.min(1, y / Math.max(1, getHeight())));
    }

    private float pressure(float value) {
        return Math.max(0, Math.min(1, value));
    }

    void cancelStroke() {
        if (penId == -1) return;
        if (segmentOpen) {
            if (remoteStroke) app.link.offerAnnotation(new byte[]{'X'}, strokeRevision);
            else page.cancel();
        }
        segmentOpen = false;
        end();
        syncPage();
    }

    private void resetNavigation() { firstFinger = secondFinger = -1; }

    private void navigate(MotionEvent event) {
        int action = event.getActionMasked();
        if (action == MotionEvent.ACTION_CANCEL || action == MotionEvent.ACTION_UP || preview.bitmap == null) {
            resetNavigation();
            return;
        }
        int first = -1, second = -1;
        for (int i = 0; i < event.getPointerCount(); i++) {
            if (event.getToolType(i) != MotionEvent.TOOL_TYPE_FINGER
                    || (action == MotionEvent.ACTION_POINTER_UP && i == event.getActionIndex())) continue;
            if (first == -1) first = i;
            else { second = i; break; }
        }
        if (first == -1) { resetNavigation(); return; }
        int firstId = event.getPointerId(first), secondId = second < 0 ? -1 : event.getPointerId(second);
        float x = event.getX(first), y = event.getY(first), span = 0;
        if (second >= 0) {
            float dx = event.getX(second) - x, dy = event.getY(second) - y;
            x += dx / 2;
            y += dy / 2;
            span = (float) Math.hypot(dx, dy);
        }
        if (action == MotionEvent.ACTION_MOVE && firstId == firstFinger && secondId == secondFinger) {
            float factor = span > 1 && touchSpan > 1 ? span / touchSpan : 1;
            viewport.transform(touchX, touchY, x, y, factor);
            postInvalidateOnAnimation();
        }
        firstFinger = firstId;
        secondFinger = secondId;
        touchX = x;
        touchY = y;
        touchSpan = span;
    }

    private void end() {
        penId = -1;
        remoteStroke = false;
        castStroke = false;
        getParent().requestDisallowInterceptTouchEvent(false);
        historyChanged.run();
    }

    @Override protected void onDetachedFromWindow() {
        cancelFingerInput();
        removeCallbacks(pump);
        scheduled = false;
        super.onDetachedFromWindow();
    }
}
