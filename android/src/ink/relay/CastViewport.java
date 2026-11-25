package ink.relay;

import android.graphics.RectF;

/** Zoom and pan in image space so the same focus survives a phone rotation. */
final class CastViewport {
    private static final float MAX_ZOOM = 8f;
    final RectF image = new RectF();
    private int width, height;
    private float fitWidth, fitHeight;
    private float centerX = .5f, centerY = .5f, zoom = 1f;

    void layout(int width, int height, int imageWidth, int imageHeight) {
        this.width = width;
        this.height = height;
        float scale = Math.min((float) width / imageWidth, (float) height / imageHeight);
        fitWidth = imageWidth * scale;
        fitHeight = imageHeight * scale;
        position();
    }

    void fit() {
        zoom = 1;
        centerX = centerY = .5f;
        position();
    }

    void fill() {
        if (fitWidth <= 0 || fitHeight <= 0) return;
        zoomAt(Math.min(MAX_ZOOM, Math.max(width / fitWidth, height / fitHeight)));
    }

    void zoomBy(float factor) { zoomAt(zoom * factor); }

    private void zoomAt(float target) {
        transform(width / 2f, height / 2f, width / 2f, height / 2f, target / zoom);
    }

    void transform(float oldX, float oldY, float newX, float newY, float factor) {
        if (image.isEmpty()) return;
        float anchorX = (oldX - image.left) / image.width();
        float anchorY = (oldY - image.top) / image.height();
        zoom = clamp(zoom * factor, 1, MAX_ZOOM);
        centerX = anchorX + (width / 2f - newX) / (fitWidth * zoom);
        centerY = anchorY + (height / 2f - newY) / (fitHeight * zoom);
        position();
    }

    private void position() {
        float w = fitWidth * zoom, h = fitHeight * zoom;
        if (w <= 0 || h <= 0) { image.setEmpty(); return; }
        float halfX = Math.min(.5f, width / (2 * w));
        float halfY = Math.min(.5f, height / (2 * h));
        centerX = clamp(centerX, halfX, 1 - halfX);
        centerY = clamp(centerY, halfY, 1 - halfY);
        float left = width / 2f - centerX * w;
        float top = height / 2f - centerY * h;
        image.set(left, top, left + w, top + h);
    }

    float imageX(float x) { return clamp((x - image.left) / image.width(), 0, 1); }
    float imageY(float y) { return clamp((y - image.top) / image.height(), 0, 1); }

    private static float clamp(float value, float minimum, float maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }
}
