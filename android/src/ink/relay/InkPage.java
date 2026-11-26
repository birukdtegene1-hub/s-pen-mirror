package ink.relay;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffXfermode;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.util.AtomicFile;
import android.util.Log;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** The same paper and ink compositor supplies the phone view and the transmitted patches. */
final class InkPage {
    enum Tool { PEN, BALLPOINT, HIGHLIGHTER, ERASER, STROKE_ERASER, CLEAR }
    static final String[] PAPERS = {"Plain", "Ruled", "Grid"};
    static final int[] COLORS = {0xff24372e, 0xff195db7, 0xff1a7957, 0xffc95845,
            0xff9447a3, 0xfff5c02f, 0xff171b1a, 0xffffffff};
    int width, height;
    final SharedPreferences settings;
    Bitmap ink;
    private Canvas inkCanvas;
    final ArrayDeque<Stroke> undo = new ArrayDeque<>(), redo = new ArrayDeque<>();
    private final ArrayDeque<Stroke> folded = new ArrayDeque<>();
    Tool tool;
    int color, size, eraserSize, paper;
    boolean fingers, strokeEraser;
    private Bitmap base, highlight;
    private Canvas mask;
    private Stroke current;
    private final Rect highlightBounds = new Rect();
    private final Rect segmentBounds = new Rect();
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint translucent = new Paint();
    private final Paint rule = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final PorterDuffXfermode erase = new PorterDuffXfermode(PorterDuff.Mode.CLEAR);
    private final AtomicFile file;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService writer = Executors.newSingleThreadExecutor();
    private boolean unsaved, saving;
    private final Runnable store = this::save;
    private static final int HISTORY = 64, RETAINED_STROKES = 512;

    InkPage(Context context, int viewWidth, int viewHeight) {
        settings = context.getSharedPreferences("ink-tools", Context.MODE_PRIVATE);
        Map<String, ?> saved = settings.getAll();
        tool = Tool.PEN;
        for (Tool option : Tool.values()) {
            if (option.ordinal() < Tool.ERASER.ordinal() && option.name().equals(saved.get("tool"))) tool = option;
        }
        color = intPreference(saved.get("color"), COLORS[0], Integer.MIN_VALUE, Integer.MAX_VALUE);
        size = intPreference(saved.get("size"), 6, 1, 36);
        eraserSize = intPreference(saved.get("eraserSize"), 10, 1, 36);
        strokeEraser = Boolean.TRUE.equals(saved.get("strokeEraser"));
        paper = intPreference(saved.get("paper"), 0, 0, PAPERS.length - 1);
        fingers = Boolean.TRUE.equals(saved.get("fingers"));
        file = new AtomicFile(new File(context.getFilesDir(), "last-ink.png"));
        try (FileInputStream input = file.openRead()) {
            base = BitmapFactory.decodeStream(input);
        } catch (IOException ignored) { }
        // A new sheet takes the shape of the writing area. Existing sheets keep their proportions.
        float scale = 1440f / Math.max(viewWidth, viewHeight);
        width = base == null ? Math.max(1, Math.round(viewWidth * scale)) : base.getWidth();
        height = base == null ? Math.max(1, Math.round(viewHeight * scale)) : base.getHeight();
        ink = bitmap(width, height);
        inkCanvas = new Canvas(ink);
        if (base != null) {
            base.setDensity(Bitmap.DENSITY_NONE);
            inkCanvas.drawBitmap(base, 0, 0, null);
        }
        paint.setStrokeCap(Paint.Cap.ROUND);
        translucent.setAlpha(92);
        rule.setColor(0xffdce3de);
        rule.setStrokeWidth(1.4f);
    }

    private static int intPreference(Object value, int fallback, int minimum, int maximum) {
        if (!(value instanceof Integer)) return fallback;
        int number = (Integer) value;
        return number >= minimum && number <= maximum ? number : fallback;
    }

    static Bitmap bitmap(int w, int h) {
        Bitmap image = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        image.setDensity(Bitmap.DENSITY_NONE);
        return image;
    }

    void saveSettings() {
        settings.edit().putString("tool", tool.name()).putInt("color", color).putInt("size", size)
                .putInt("eraserSize", eraserSize).putBoolean("strokeEraser", strokeEraser)
                .putInt("paper", paper).putBoolean("fingers", fingers).apply();
    }

    Tool eraserTool() { return strokeEraser ? Tool.STROKE_ERASER : Tool.ERASER; }

    void draw(Canvas canvas) {
        canvas.drawColor(0xfffffefa);
        if (paper != 0) {
            for (int y = 48; y < height; y += 48) canvas.drawLine(0, y, width, y, rule);
            if (paper == 2) {
                for (int x = 48; x < width; x += 48) canvas.drawLine(x, 0, x, height, rule);
            }
        }
        canvas.drawBitmap(ink, 0, 0, null);
        if (current != null && current.tool == Tool.HIGHLIGHTER)
            canvas.drawBitmap(highlight, 0, 0, translucent);
    }

    void begin(boolean useEraser) {
        current = new Stroke(useEraser ? eraserTool() : tool, color, useEraser ? eraserSize : size);
        if (current.tool == Tool.STROKE_ERASER) current.candidates = editableStrokes();
        if (current.tool == Tool.HIGHLIGHTER) {
            clearMask();
            highlightBounds.setEmpty();
        }
    }

    void point(float x, float y, float pressure, Rect dirty) {
        if (current == null) return;
        Stroke s = current;
        int i = s.length;
        x = Math.max(0, Math.min(width - 1, x));
        y = Math.max(0, Math.min(height - 1, y));
        if (i >= 3 && s.data[i - 3] == x && s.data[i - 2] == y
                && (s.tool != Tool.PEN || s.data[i - 1] == pressure)) return;
        s.add(x, y, pressure);
        float lastX = i > 0 ? s.data[i - 3] : x, lastY = i > 0 ? s.data[i - 2] : y;
        if (s.tool == Tool.STROKE_ERASER) {
            float endX = x, endY = y;
            if (s.candidates.removeIf(ink -> {
                if (!ink.touches(lastX, lastY, endX, endY, s.size * 1.75f)) return false;
                s.removed.add(ink);
                return true;
            })) {
                replay();
                dirty.set(0, 0, width, height);
            }
            return;
        }
        float w = segment(inkCanvas, s, i);
        float r = w / 2 + 2;
        segmentBounds.set((int) Math.floor(Math.min(x, lastX) - r), (int) Math.floor(Math.min(y, lastY) - r),
                (int) Math.ceil(Math.max(x, lastX) + r), (int) Math.ceil(Math.max(y, lastY) + r));
        dirty.union(segmentBounds);
        if (s.tool == Tool.HIGHLIGHTER) highlightBounds.union(segmentBounds);
    }

    private float segment(Canvas target, Stroke s, int i) {
        float x = s.data[i], y = s.data[i + 1], p = s.data[i + 2];
        boolean stationary = i == 0 || (s.data[i - 3] == x && s.data[i - 2] == y);
        if (!stationary) p = (p + s.data[i - 1]) * 0.5f;
        float w = s.size;
        if (s.tool == Tool.PEN) w *= 0.23f + 1.35f * Math.min(1, Math.max(0, p));
        if (s.tool == Tool.HIGHLIGHTER) { w *= 3; target = mask; }
        if (s.tool == Tool.ERASER) w *= 3.5f;
        paint.setXfermode(s.tool == Tool.ERASER ? erase : null);
        paint.setColor(s.color);
        paint.setStrokeWidth(w);
        if (stationary) target.drawCircle(x, y, w / 2, paint);
        else target.drawLine(s.data[i - 3], s.data[i - 2], x, y, paint);
        return w;
    }

    private void clearMask() {
        if (highlight == null) {
            highlight = bitmap(width, height);
            mask = new Canvas(highlight);
        } else highlight.eraseColor(Color.TRANSPARENT);
    }

    void finish(Rect dirty) {
        if (current == null) return;
        if (current.tool == Tool.HIGHLIGHTER) {
            inkCanvas.drawBitmap(highlight, 0, 0, translucent);
            // Flattening onto transparent ink can round alpha differently from the live paper preview.
            dirty.union(highlightBounds);
        }
        Stroke completed = current;
        current = null;
        completed.candidates = null;
        if (completed.tool == Tool.STROKE_ERASER && completed.removed.isEmpty()) return;
        remember(completed);
    }

    void cancel() {
        if (current == null) return;
        current = null;
        replay();
    }

    void clear() {
        current = null;
        ink.eraseColor(Color.TRANSPARENT);
        remember(new Stroke(Tool.CLEAR, 0, 0));
    }

    void rotateClockwise() {
        int oldHeight = height;
        height = width;
        width = oldHeight;
        if (base != null) {
            Bitmap rotated = bitmap(width, height);
            Canvas c = new Canvas(rotated);
            c.translate(width, 0);
            c.rotate(90);
            c.drawBitmap(base, 0, 0, null);
            base = rotated;
        }
        for (Stroke stroke : undo) stroke.rotate(oldHeight);
        for (Stroke stroke : redo) stroke.rotate(oldHeight);
        for (Stroke stroke : folded) stroke.rotate(oldHeight);
        highlight = null;
        mask = null;
        ink = bitmap(width, height);
        inkCanvas = new Canvas(ink);
        replay();
        changed();
    }

    private void remember(Stroke stroke) {
        undo.addLast(stroke);
        redo.clear();
        if (undo.size() > HISTORY) folded.addLast(undo.removeFirst());
        while (folded.size() > RETAINED_STROKES) {
            Stroke oldest = folded.peekFirst();
            if (undo.stream().anyMatch(edit -> edit.removed.contains(oldest))) break;
            if (base == null) base = bitmap(width, height);
            else if (!base.isMutable()) base = base.copy(Bitmap.Config.ARGB_8888, true);
            folded.removeFirst();
            if (folded.stream().noneMatch(edit -> edit.removed.contains(oldest))) render(new Canvas(base), oldest);
        }
        changed();
    }

    void step(boolean forward) {
        ArrayDeque<Stroke> from = forward ? redo : undo, to = forward ? undo : redo;
        if (from.isEmpty()) return;
        to.addLast(from.removeLast());
        replay();
        changed();
    }

    private void render(Canvas target, Stroke stroke) {
        if (stroke.tool == Tool.STROKE_ERASER) return;
        if (stroke.tool == Tool.CLEAR) {
            target.drawColor(Color.TRANSPARENT, PorterDuff.Mode.CLEAR);
            return;
        }
        if (stroke.tool == Tool.HIGHLIGHTER) clearMask();
        for (int i = 0; i < stroke.length; i += 3) segment(target, stroke, i);
        if (stroke.tool == Tool.HIGHLIGHTER) target.drawBitmap(highlight, 0, 0, translucent);
    }

    private void replay() {
        ink.eraseColor(Color.TRANSPARENT);
        if (base != null) inkCanvas.drawBitmap(base, 0, 0, null);
        HashSet<Stroke> hidden = new HashSet<>();
        for (Stroke s : folded) hidden.addAll(s.removed);
        for (Stroke s : undo) hidden.addAll(s.removed);
        if (current != null) hidden.addAll(current.removed);
        for (Stroke s : folded) if (!hidden.contains(s)) render(inkCanvas, s);
        for (Stroke s : undo) if (!hidden.contains(s)) render(inkCanvas, s);
    }

    private ArrayList<Stroke> editableStrokes() {
        ArrayList<Stroke> result = new ArrayList<>();
        for (Stroke s : folded) applyVisibility(result, s);
        for (Stroke s : undo) applyVisibility(result, s);
        return result;
    }

    private static void applyVisibility(ArrayList<Stroke> visible, Stroke operation) {
        if (operation.tool == Tool.CLEAR) visible.clear();
        else if (operation.tool == Tool.STROKE_ERASER) visible.removeAll(operation.removed);
        else if (operation.tool != Tool.ERASER) visible.add(operation);
    }

    private void changed() {
        unsaved = true;
        main.removeCallbacks(store);
        main.postDelayed(store, 650);
    }

    void save() {
        if (saving || !unsaved || current != null) return;
        Bitmap snapshot = ink.copy(Bitmap.Config.ARGB_8888, false);
        saving = true;
        unsaved = false;
        writer.execute(() -> {
            FileOutputStream output = null;
            boolean ok = false;
            try {
                output = file.startWrite();
                if (!snapshot.compress(Bitmap.CompressFormat.PNG, 100, output)) throw new IOException("PNG");
                file.finishWrite(output);
                ok = true;
            } catch (IOException e) {
                file.failWrite(output);
                Log.w("S-Pen Mirror", "Could not save the last canvas", e);
            } finally {
                snapshot.recycle();
            }
            final boolean saved = ok;
            main.post(() -> {
                saving = false;
                if (!saved) unsaved = true;
                else if (unsaved) main.postDelayed(store, 200);
            });
        });
    }

    private static final class Stroke {
        final Tool tool;
        final int color, size;
        float[] data = new float[96];
        int length;
        final HashSet<Stroke> removed = new HashSet<>();
        ArrayList<Stroke> candidates;
        Stroke(Tool tool, int color, int size) { this.tool = tool; this.color = color; this.size = size; }
        void add(float x, float y, float p) {
            if (length + 3 > data.length) data = Arrays.copyOf(data, data.length * 2);
            data[length++] = x;
            data[length++] = y;
            data[length++] = p;
        }
        void rotate(int height) {
            for (int i = 0; i < length; i += 3) {
                float x = data[i];
                data[i] = height - 1 - data[i + 1];
                data[i + 1] = x;
            }
        }

        boolean touches(float ax, float ay, float bx, float by, float radius) {
            float extra = tool == Tool.HIGHLIGHTER ? size * 1.5f : size * .5f;
            float hit = radius + extra;
            for (int i = 0; i < length; i += 3) {
                int previous = Math.max(0, i - 3);
                float cx = data[previous], cy = data[previous + 1], dx = data[i], dy = data[i + 1];
                if (Math.max(ax, bx) + hit < Math.min(cx, dx) || Math.max(cx, dx) + hit < Math.min(ax, bx)
                        || Math.max(ay, by) + hit < Math.min(cy, dy) || Math.max(cy, dy) + hit < Math.min(ay, by)) continue;
                float bound = hit * hit;
                float cross = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx);
                if (Math.abs(cross) > .00001f) {
                    float along = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / cross;
                    float across = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / cross;
                    if (along >= 0 && along <= 1 && across >= 0 && across <= 1) return true;
                }
                if (distance(ax, ay, cx, cy, dx, dy) <= bound || distance(bx, by, cx, cy, dx, dy) <= bound
                        || distance(cx, cy, ax, ay, bx, by) <= bound || distance(dx, dy, ax, ay, bx, by) <= bound) return true;
            }
            return false;
        }

        private static float distance(float x, float y, float ax, float ay, float bx, float by) {
            float dx = bx - ax, dy = by - ay;
            float length = dx * dx + dy * dy;
            float t = length == 0 ? 0 : Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / length));
            float offsetX = x - ax - t * dx, offsetY = y - ay - t * dy;
            return offsetX * offsetX + offsetY * offsetY;
        }
    }
}
