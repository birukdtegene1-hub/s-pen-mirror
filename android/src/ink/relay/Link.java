package ink.relay;

import android.graphics.Bitmap;
import android.os.Handler;
import android.os.Looper;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** One writer owns the TLS stream. UI and capture threads only publish bitmaps. */
final class Link {
    enum State { OFFLINE, CONNECTING, ONLINE }
    final Handler main = new Handler(Looper.getMainLooper());
    private final ArrayList<Runnable> listeners = new ArrayList<>();
    volatile State state = State.OFFLINE;
    volatile boolean screen;
    volatile String status = "Local canvas";
    volatile Endpoint endpoint;
    private int generation;
    private Socket socket;
    private Frame pending;
    private int control;

    void listen(Runnable listener) { listeners.add(listener); }
    void unlisten(Runnable listener) { listeners.remove(listener); }

    private void changed() {
        main.post(() -> {
            for (Runnable listener : new ArrayList<>(listeners)) listener.run();
        });
    }

    synchronized void connect(Endpoint target) {
        reset();
        endpoint = target;
        state = State.CONNECTING;
        status = "Connecting to " + target.host + "…";
        int session = generation;
        changed();
        new Thread(() -> run(session, target), "pen-link").start();
    }

    synchronized void disconnect() {
        reset();
        status = "Disconnected";
        changed();
    }

    private void reset() {
        generation++;
        state = State.OFFLINE;
        screen = false;
        dropPending();
        control = 0;
        if (socket != null) {
            try { socket.close(); } catch (IOException ignored) { }
            socket = null;
        }
        notifyAll();
    }

    synchronized boolean setScreen(boolean enabled, String message) {
        if (enabled && state != State.ONLINE) return false;
        if (screen != enabled) {
            screen = enabled;
            dropPending();
            control = enabled ? 'M' : 'B';
            notifyAll();
        }
        if (state == State.ONLINE) {
            status = message == null ? "Paired · " + endpoint.host : message;
        }
        changed();
        return true;
    }

    synchronized boolean ready(boolean forScreen) {
        return state == State.ONLINE && screen == forScreen && pending == null;
    }

    /** Consumes ownership of bitmap, including when the stream changes during a capture. */
    synchronized boolean offer(char kind, Bitmap bitmap, int left, int top) {
        if (!ready(kind == 'S')) {
            bitmap.recycle();
            return false;
        }
        pending = new Frame(kind, bitmap, left, top);
        notifyAll();
        return true;
    }

    private void dropPending() {
        if (pending != null) pending.bitmap.recycle();
        pending = null;
    }

    private static SSLContext pinnedContext(byte[] pin) throws Exception {
        X509TrustManager pinned = new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] chain, String type) throws CertificateException {
                throw new CertificateException("Client certificates unsupported");
            }
            public void checkServerTrusted(X509Certificate[] chain, String type) throws CertificateException {
                try {
                    if (chain == null || chain.length == 0 || !MessageDigest.isEqual(pin,
                            MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded()))) {
                        throw new CertificateException("PC certificate does not match the pairing code");
                    }
                } catch (java.security.NoSuchAlgorithmException e) {
                    throw new CertificateException(e);
                }
            }
        };
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, new TrustManager[]{pinned}, null);
        return ctx;
    }

    private void run(int session, Endpoint target) {
        String error = "Connection lost. Tap Pair PC to reconnect.";
        try (Socket raw = new Socket()) {
            synchronized (this) {
                if (session != generation) return;
                socket = raw;
            }
            raw.connect(new InetSocketAddress(target.host, target.port), 6500);
            raw.setSoTimeout(8000);
            raw.setTcpNoDelay(true);
            try (SSLSocket tls = (SSLSocket) pinnedContext(target.pin).getSocketFactory()
                    .createSocket(raw, target.host, target.port, true)) {
                tls.setSoTimeout(8000);
                tls.startHandshake();
                InputStream in = tls.getInputStream();
                DataOutputStream out = new DataOutputStream(new BufferedOutputStream(tls.getOutputStream()));
                out.write(("SPEN1 " + target.token + "\n").getBytes(StandardCharsets.US_ASCII));
                out.flush();
                int answer = in.read();
                if (answer != '+') {
                    error = answer == '*' ? "Another phone is paired with this PC." : "Pairing expired. Scan the PC again.";
                    return;
                }
                synchronized (this) {
                    if (generation != session) return;
                    state = State.ONLINE;
                    status = "Paired · " + target.host;
                    control = 'B';
                    changed();
                }
                while (true) {
                    Frame frame = null;
                    int kind;
                    synchronized (this) {
                        if (session == generation && control == 0 && pending == null) wait(2000);
                        if (session != generation) return;
                        if (control != 0) {
                            kind = control;
                            control = 0;
                        } else {
                            frame = pending;
                            pending = null;
                            kind = frame == null ? 'H' : frame.kind;
                        }
                    }
                    // Socket read timeouts cover acknowledgments. This deadline also covers a blocked write.
                    Runnable deadline = () -> {
                        synchronized (Link.this) {
                            if (generation == session && socket == raw) {
                                try { raw.close(); } catch (IOException ignored) { }
                            }
                        }
                    };
                    main.postDelayed(deadline, 12000);
                    try {
                        out.writeByte(kind);
                        if (frame == null) out.writeInt(0);
                        else frame.write(out);
                        out.flush();
                        if (in.read() != '+') throw new IOException("PC disconnected");
                    } finally {
                        main.removeCallbacks(deadline);
                        if (frame != null) frame.bitmap.recycle();
                    }
                }
            }
        } catch (javax.net.ssl.SSLHandshakeException e) {
            error = "PC identity changed. Scan the new pairing code.";
        } catch (Exception e) {
            if (state == State.CONNECTING) error = "PC unreachable. Check Wi-Fi, PC address and firewall.";
        } finally {
            synchronized (this) {
                if (session == generation) {
                    reset();
                    status = error;
                    changed();
                }
            }
        }
    }

    private static final class Frame {
        final char kind;
        final Bitmap bitmap;
        final int left, top;
        Frame(char kind, Bitmap bitmap, int left, int top) {
            this.kind = kind;
            this.bitmap = bitmap;
            this.left = left;
            this.top = top;
        }

        void write(DataOutputStream out) throws IOException {
            ByteArrayOutputStream data = new ByteArrayOutputStream();
            boolean video = kind == 'S';
            if (!bitmap.compress(video ? Bitmap.CompressFormat.JPEG : Bitmap.CompressFormat.PNG,
                    video ? 80 : 100, data)) throw new IOException("Could not encode picture");
            out.writeInt(data.size() + (kind == 'P' ? 4 : 0));
            if (kind == 'P') {
                out.writeShort(left);
                out.writeShort(top);
            }
            data.writeTo(out);
        }
    }
}
