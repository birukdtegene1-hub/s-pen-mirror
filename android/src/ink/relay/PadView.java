package ink.relay;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Rect;
import android.os.Build;
import android.view.MotionEvent;
import android.view.View;

final class PadView extends View {
    private final RelayApp app;
    private final Runnable historyChanged;
    InkPage page;
    private final Rect dirty = new Rect();
    private boolean full = true, scheduled;
    private int penId = -1;
    private float scale, left, top;

    PadView(Context ctx, Runnable historyChanged) {
        super(ctx);
        app = (RelayApp) ctx.getApplicationContext();
        this.historyChanged = historyChanged;
        setContentDescription("S-Pen writing area");
        setFocusable(true);
    }

    @Override protected void onSizeChanged(int w, int h, int oldW, int oldH) {
        if (w <= 0 || h <= 0) return;
        page = app.page(w, h);
        fitPage(w, h);
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
        canvas.drawColor(0xffdce1d8);
        if (page == null) return;
        canvas.save();
        canvas.translate(left, top);
        canvas.scale(scale, scale);
        canvas.clipRect(0, 0, page.width, page.height);
        page.draw(canvas);
        canvas.restore();
    }

    void syncPage() {
        full = true;
        update();
    }

    private void update() {
        postInvalidateOnAnimation();
        if (!scheduled && isAttachedToWindow() && app.link.state == Link.State.ONLINE && !app.link.screen) {
            scheduled = true;
            postOnAnimation(pump);
        }
    }

    private final Runnable pump = new Runnable() {
        @Override public void run() {
            scheduled = false;
            if (page == null || !isShown() || app.link.screen || app.link.state != Link.State.ONLINE) return;
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
        if (action == MotionEvent.ACTION_DOWN || action == MotionEvent.ACTION_POINTER_DOWN) {
            int type = event.getToolType(actionIndex);
            boolean stylus = type == MotionEvent.TOOL_TYPE_STYLUS || type == MotionEvent.TOOL_TYPE_ERASER;
            if (penId != -1 || (!stylus && !page.fingers)) return true;
            float x = (event.getX(actionIndex) - left) / scale, y = (event.getY(actionIndex) - top) / scale;
            if (x < 0 || x >= page.width || y < 0 || y >= page.height) return true;
            penId = event.getPointerId(actionIndex);
            page.begin(type == MotionEvent.TOOL_TYPE_ERASER ||
                    (event.getButtonState() & MotionEvent.BUTTON_STYLUS_PRIMARY) != 0);
            add(event, actionIndex, -1);
            getParent().requestDisallowInterceptTouchEvent(true);
        } else if (action == MotionEvent.ACTION_MOVE && penId != -1) {
            int idx = event.findPointerIndex(penId);
            if (idx != -1) {
                for (int i = 0; i < event.getHistorySize(); i++) add(event, idx, i);
                add(event, idx, -1);
            }
        } else if ((action == MotionEvent.ACTION_POINTER_UP || action == MotionEvent.ACTION_UP)
                && penId == event.getPointerId(actionIndex)) {
            if (Build.VERSION.SDK_INT >= 33 && (event.getFlags() & MotionEvent.FLAG_CANCELED) != 0) cancelStroke();
            else {
                add(event, actionIndex, -1);
                page.finish(dirty);
                end();
            }
        } else if (action == MotionEvent.ACTION_CANCEL) cancelStroke();
        update();
        return true;
    }

    private void add(MotionEvent e, int idx, int history) {
        float x = history < 0 ? e.getX(idx) : e.getHistoricalX(idx, history);
        float y = history < 0 ? e.getY(idx) : e.getHistoricalY(idx, history);
        float p = history < 0 ? e.getPressure(idx) : e.getHistoricalPressure(idx, history);
        page.point((x - left) / scale, (y - top) / scale, p, dirty);
    }

    void cancelStroke() {
        if (penId == -1) return;
        page.cancel();
        end();
        syncPage();
    }

    private void end() {
        penId = -1;
        getParent().requestDisallowInterceptTouchEvent(false);
        historyChanged.run();
    }

    @Override protected void onDetachedFromWindow() {
        removeCallbacks(pump);
        scheduled = false;
        super.onDetachedFromWindow();
    }
}
