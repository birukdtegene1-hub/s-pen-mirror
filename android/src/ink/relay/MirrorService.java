package ink.relay;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.view.Display;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/** The capture grant and virtual display live only for the current screen-sharing session. */
public final class MirrorService extends Service {
    static final String STOP = "ink.relay.STOP";
    private static final int NOTICE = 21;
    private Link link;
    private HandlerThread thread;
    private Handler worker;
    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private DisplayManager displays;
    private int width, height, density;
    private long lastFrame;
    private boolean started, queued;
    private volatile boolean ending;
    private volatile String message;
    private int[] argb;
    private final Runnable checkLink = () -> {
        if (started && (link.state != Link.State.ONLINE || !link.screen)) stopSelf();
    };
    private final MediaProjection.Callback callback = new MediaProjection.Callback() {
        @Override public void onStop() { finishCapture(null); }
        @Override public void onCapturedContentResize(int w, int h) { resize(w, h); }
    };
    private final DisplayManager.DisplayListener rotation = new DisplayManager.DisplayListener() {
        public void onDisplayAdded(int id) { }
        public void onDisplayRemoved(int id) { }
        public void onDisplayChanged(int id) {
            if (id == Display.DEFAULT_DISPLAY && Build.VERSION.SDK_INT < 34 && !ending) {
                DisplayMetrics metrics = metrics();
                resize(metrics.widthPixels, metrics.heightPixels);
            }
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        link = ((RelayApp) getApplication()).link;
        link.listen(checkLink);
        displays = getSystemService(DisplayManager.class);
        thread = new HandlerThread("screen-capture");
        thread.start();
        worker = new Handler(thread.getLooper());
    }

    @Override public int onStartCommand(Intent intent, int flags, int id) {
        if (intent == null || STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (started) return START_NOT_STICKY;
        try {
            Intent grant = intent.getParcelableExtra("grant");
            NotificationManager manager = getSystemService(NotificationManager.class);
            manager.createNotificationChannel(new NotificationChannel("screen", "Screen mirroring",
                    NotificationManager.IMPORTANCE_LOW));
            PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, MirrorService.class).setAction(STOP),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            Notification notification = new Notification.Builder(this, "screen")
                    .setSmallIcon(R.drawable.ic_pen).setContentTitle("S-Pen Mirror is sharing your screen")
                    .setContentText("Tap to open S-Pen Mirror.").setOngoing(true)
                    .setContentIntent(open).addAction(new Notification.Action.Builder(null, "Stop sharing", stop).build())
                    .build();
            startForeground(NOTICE, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
            if (grant == null || !link.setScreen(true, null)) { stopSelf(); return START_NOT_STICKY; }
            started = true;
            worker.post(() -> begin(grant));
        } catch (RuntimeException e) {
            finishCapture("Screen sharing could not start. Try Screen mirror again.");
        }
        return START_NOT_STICKY;
    }

    @SuppressWarnings("deprecation")
    private DisplayMetrics metrics() {
        DisplayMetrics m = new DisplayMetrics();
        displays.getDisplay(Display.DEFAULT_DISPLAY).getRealMetrics(m);
        return m;
    }

    private void begin(Intent grant) {
        if (ending) return;
        try {
            projection = getSystemService(MediaProjectionManager.class)
                    .getMediaProjection(android.app.Activity.RESULT_OK, grant);
            if (projection == null) throw new IllegalStateException();
            projection.registerCallback(callback, worker);
            DisplayMetrics m = metrics();
            density = m.densityDpi;
            resize(m.widthPixels, m.heightPixels);
            if (Build.VERSION.SDK_INT < 34) displays.registerDisplayListener(rotation, worker);
        } catch (RuntimeException e) {
            finishCapture("Android ended the screen capture. Tap Screen mirror to start again.");
        }
    }

    private void resize(int w, int h) {
        if (ending || projection == null || w <= 0 || h <= 0) return;
        float scale = Math.min(1f, 1280f / Math.max(w, h));
        int newW = Math.max(1, Math.round(w * scale)), newH = Math.max(1, Math.round(h * scale));
        if (newW == width && newH == height) return;
        ImageReader old = reader;
        try {
            width = newW;
            height = newH;
            argb = null;
            reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 3);
            reader.setOnImageAvailableListener(source -> {
                if (source != reader || ending || queued) return;
                queued = true;
                worker.postAtTime(readFrame, Math.max(SystemClock.uptimeMillis(), lastFrame + 66));
            }, worker);
            if (display == null) {
                display = projection.createVirtualDisplay("S-Pen Mirror", width, height, density,
                        DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.getSurface(), null, worker);
                if (display == null) throw new IllegalStateException();
            } else {
                display.setSurface(null);
                display.resize(width, height, density);
                display.setSurface(reader.getSurface());
            }
        } catch (RuntimeException e) {
            finishCapture("Android could not create the screen capture surface.");
        } finally {
            if (old != null && old != reader) old.close();
        }
    }

    private final Runnable readFrame = new Runnable() {
        @Override public void run() {
            if (ending || reader == null || !link.screen) { queued = false; return; }
            if (!link.ready(true)) {
                worker.postDelayed(this, 25);
                return;
            }
            queued = false;
            try (Image frame = reader.acquireLatestImage()) {
                if (frame == null) return;
                Image.Plane plane = frame.getPlanes()[0];
                Bitmap bitmap = copyPixels(plane.getBuffer(), plane.getRowStride(), plane.getPixelStride());
                link.offer('S', bitmap, 0, 0);
                lastFrame = SystemClock.uptimeMillis();
            } catch (RuntimeException e) {
                finishCapture("Screen capture interrupted. Tap Screen mirror to restart.");
            }
        }
    };

    Bitmap copyPixels(ByteBuffer pixels, int stride, int pixelStride) {
        if (pixelStride != 4) throw new IllegalStateException("Unexpected RGBA pixel stride");
        if (argb == null) argb = new int[width * height];
        // Read the plane's RGBA byte order explicitly. Bitmap's native byte order varies by renderer.
        pixels.order(ByteOrder.BIG_ENDIAN);
        int start = pixels.position(), index = 0;
        for (int y = 0; y < height; y++) {
            int offset = start + y * stride;
            for (int x = 0; x < width; x++, offset += 4) {
                argb[index++] = Integer.rotateRight(pixels.getInt(offset), 8);
            }
        }
        return Bitmap.createBitmap(argb, width, height, Bitmap.Config.ARGB_8888);
    }

    private void finishCapture(String reason) {
        if (ending) return;
        message = reason;
        stopSelf();
    }

    @Override public void onDestroy() {
        ending = true;
        link.unlisten(checkLink);
        if (started || message != null) link.setScreen(false, message);
        stopForeground(STOP_FOREGROUND_REMOVE);
        worker.post(() -> {
            worker.removeCallbacksAndMessages(null);
            displays.unregisterDisplayListener(rotation);
            if (display != null) display.release();
            if (reader != null) reader.close();
            if (projection != null) {
                projection.unregisterCallback(callback);
                projection.stop();
            }
            argb = null;
            thread.quitSafely();
        });
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }
}
