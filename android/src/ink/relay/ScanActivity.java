package ink.relay;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.hardware.Camera;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.ReaderException;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

@SuppressWarnings("deprecation")
public final class ScanActivity extends Activity implements SurfaceHolder.Callback {
    static final int ENTER_LINK = RESULT_FIRST_USER;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private SurfaceView preview;
    private FrameLayout previewArea;
    private TextView hint;
    private SurfaceHolder surface;
    private Camera camera;
    private Camera.Size previewSize;
    private int previewRotation;
    private boolean resumed;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xff16221b);
        preview = new SurfaceView(this);
        preview.getHolder().addCallback(this);
        previewArea = new FrameLayout(this);
        previewArea.addView(preview, new FrameLayout.LayoutParams(-1, -1, Gravity.CENTER));
        previewArea.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> fitPreview());
        root.addView(previewArea);
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackgroundColor(0xf5f3f5ef);
        Ui.pad(panel, 18, 12);
        panel.addView(Ui.text(this, "Pair with your PC", 22, Ui.INK, true));
        hint = Ui.text(this, "Point the camera at the QR code in the PC window.", 15, Ui.MUTED, false);
        panel.addView(hint);
        LinearLayout buttons = Ui.row(this);
        Button enter = Ui.button(this, "Enter pairing link", () -> { setResult(ENTER_LINK); finish(); });
        Ui.item(buttons, enter, 1, 46);
        Ui.item(buttons, Ui.button(this, "Cancel", this::finish), 1, 46);
        panel.addView(buttons);
        root.addView(panel, new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM));
        setContentView(root);
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, 1);
        }
    }

    @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(code, permissions, results);
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) start();
        else hint.setText("Camera access is needed to scan. You can also enter the pairing link.");
    }
    public void surfaceCreated(SurfaceHolder holder) { surface = holder; start(); }
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) { }
    public void surfaceDestroyed(SurfaceHolder holder) { closeCamera(); surface = null; }
    @Override protected void onResume() { super.onResume(); resumed = true; start(); }
    @Override protected void onPause() { resumed = false; closeCamera(); super.onPause(); }

    private void start() {
        if (surface == null || camera != null || !resumed
                || checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return;
        try {
            Camera.CameraInfo info = new Camera.CameraInfo();
            int id = 0;
            for (int i = 0; i < Camera.getNumberOfCameras(); i++) {
                Camera.getCameraInfo(i, info);
                if (info.facing == Camera.CameraInfo.CAMERA_FACING_BACK) { id = i; break; }
            }
            Camera.getCameraInfo(id, info);
            camera = Camera.open(id);
            Camera.Parameters params = camera.getParameters();
            List<String> focus = params.getSupportedFocusModes();
            if (focus != null && focus.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE))
                params.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
            Camera.Size best = null;
            for (Camera.Size s : params.getSupportedPreviewSizes()) {
                if (best == null || Math.abs(s.width * s.height - 921600) < Math.abs(best.width * best.height - 921600))
                    best = s;
            }
            if (best != null) params.setPreviewSize(best.width, best.height);
            params.setPreviewFormat(ImageFormat.NV21);
            camera.setParameters(params);
            previewSize = camera.getParameters().getPreviewSize();
            int rotation = getWindowManager().getDefaultDisplay().getRotation() * 90;
            int degrees = info.facing == Camera.CameraInfo.CAMERA_FACING_BACK
                    ? (info.orientation - rotation + 360) % 360 : (360 - (info.orientation + rotation) % 360) % 360;
            camera.setDisplayOrientation(degrees);
            previewRotation = degrees;
            fitPreview();
            camera.setPreviewDisplay(surface);
            camera.startPreview();
            next();
        } catch (Exception e) {
            closeCamera();
            hint.setText("Camera unavailable. Close other camera apps or enter a pairing link.");
        }
    }

    private void fitPreview() {
        if (previewSize == null || previewArea.getWidth() == 0 || previewArea.getHeight() == 0) return;
        boolean rotated = previewRotation % 180 != 0;
        int width = rotated ? previewSize.height : previewSize.width;
        int height = rotated ? previewSize.width : previewSize.height;
        float scale = Math.min((float) previewArea.getWidth() / width, (float) previewArea.getHeight() / height);
        FrameLayout.LayoutParams bounds = (FrameLayout.LayoutParams) preview.getLayoutParams();
        int fittedWidth = Math.max(1, Math.round(width * scale));
        int fittedHeight = Math.max(1, Math.round(height * scale));
        if (bounds.width != fittedWidth || bounds.height != fittedHeight) {
            bounds.width = fittedWidth;
            bounds.height = fittedHeight;
            preview.setLayoutParams(bounds);
        }
    }

    private void next() {
        Camera active = camera;
        Camera.Size size = previewSize;
        if (active == null || size == null || !resumed || isFinishing()) return;
        try {
            active.setOneShotPreviewCallback((data, source) -> {
                if (source != camera || !resumed || isFinishing()) return;
                if (data == null || data.length < size.width * size.height) {
                    main.postDelayed(this::next, 110);
                    return;
                }
                try { worker.execute(() -> decode(source, data, size)); }
                catch (RejectedExecutionException ignored) { }
            });
        } catch (RuntimeException e) {
            closeCamera();
            hint.setText("Camera unavailable. You can enter a pairing link.");
        }
    }

    private void decode(Camera source, byte[] data, Camera.Size size) {
        String text = null;
        try {
            PlanarYUVLuminanceSource pixels = new PlanarYUVLuminanceSource(
                    data, size.width, size.height, 0, 0, size.width, size.height, false);
            text = new QRCodeReader().decode(new BinaryBitmap(new HybridBinarizer(pixels))).getText();
        } catch (ReaderException ignored) { }
        final String link = text;
        main.post(() -> {
            if (source != camera || !resumed || isFinishing()) return;
            if (link != null) {
                try {
                    Endpoint.parse(link);
                    setResult(RESULT_OK, new Intent().putExtra("link", link));
                    finish();
                    return;
                } catch (IllegalArgumentException e) { hint.setText(e.getMessage()); }
            }
            main.postDelayed(this::next, 110);
        });
    }

    private void closeCamera() {
        main.removeCallbacksAndMessages(null);
        Camera old = camera;
        camera = null;
        previewSize = null;
        if (old != null) {
            try { old.setPreviewCallback(null); }
            catch (RuntimeException ignored) { }
            old.release();
        }
    }

    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
