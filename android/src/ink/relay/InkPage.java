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
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** The same paper and ink compositor supplies the phone view and the transmitted patches. */
final class InkPage {
    enum Tool { PEN, BALLPOINT, HIGHLIGHTER, ERASER, CLEAR }
    static final String[] PAPERS = {"Plain", "Ruled", "Grid"};
    static final int[] COLORS = {0xff24372e, 0xff195db7, 0xff1a7957, 0xffc95845,
            0xff9447a3, 0xfff5c02f, 0xff171b1a, 0xffffffff};
    int width, height;
    final SharedPreferences settings;
    Bitmap ink;
    private Canvas inkCanvas;
    final ArrayDeque<Stroke> undo = new ArrayDeque<>(), redo = new ArrayDeque<>();
    Tool tool;
    int color, size, paper;
    boolean fingers;
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
    private static final int HISTORY = 64;

    InkPage(Context context, int viewWidth, int viewHeight) {
        settings = context.getSharedPreferences("ink-tools", Context.MODE_PRIVATE);
        tool = Tool.valueOf(settings.getString("tool", "PEN"));
        color = settings.getInt("color", COLORS[0]);
        size = settings.getInt("size", 6);
        paper = settings.getInt("paper", 0);
        fingers = settings.getBoolean("fingers", false);
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

    static Bitmap bitmap(int w, int h) {
        Bitmap image = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        image.setDensity(Bitmap.DENSITY_NONE);
        return image;
    }

    void saveSettings() {
        settings.edit().putString("tool", tool.name()).putInt("color", color).putInt("size", size)
                .putInt("paper", paper).putBoolean("fingers", fingers).apply();
    }

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
        current = new Stroke(useEraser ? Tool.ERASER : tool, color, size);
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
        if (i >= 3 && s.data[i - 3] == x && s.data[i - 2] == y) return;
        s.add(x, y, pressure);
        float w = segment(inkCanvas, s, i);
        float lastX = i > 0 ? s.data[i - 3] : x, lastY = i > 0 ? s.data[i - 2] : y;
        float r = w / 2 + 2;
        segmentBounds.set((int) Math.floor(Math.min(x, lastX) - r), (int) Math.floor(Math.min(y, lastY) - r),
                (int) Math.ceil(Math.max(x, lastX) + r), (int) Math.ceil(Math.max(y, lastY) + r));
        dirty.union(segmentBounds);
        if (s.tool == Tool.HIGHLIGHTER) highlightBounds.union(segmentBounds);
    }

    private float segment(Canvas target, Stroke s, int i) {
        float x = s.data[i], y = s.data[i + 1], p = s.data[i + 2];
        if (i > 0) p = (p + s.data[i - 1]) * 0.5f;
        float w = s.size;
        if (s.tool == Tool.PEN) w *= 0.23f + 1.35f * Math.min(1, Math.max(0, p));
        if (s.tool == Tool.HIGHLIGHTER) { w *= 3; target = mask; }
        if (s.tool == Tool.ERASER) w *= 3.5f;
        paint.setXfermode(s.tool == Tool.ERASER ? erase : null);
        paint.setColor(s.color);
        paint.setStrokeWidth(w);
        if (i == 0) target.drawCircle(x, y, w / 2, paint);
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
        if (undo.size() > HISTORY) {
            if (base == null) base = bitmap(width, height);
            else if (!base.isMutable()) base = base.copy(Bitmap.Config.ARGB_8888, true);
            render(new Canvas(base), undo.removeFirst());
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
        for (Stroke s : undo) render(inkCanvas, s);
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
                data[i] = height - data[i + 1];
                data[i + 1] = x;
            }
        }
    }
}
