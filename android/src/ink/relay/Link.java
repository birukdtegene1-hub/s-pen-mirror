package ink.relay;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import java.security.MessageDigest;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.ArrayDeque;
import java.util.ArrayList;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** One worker owns the TLS stream and publishes the most recent PC frame. */
final class Link {
    private static final String CAST_PROTOCOL = "spen-region-1";
    private static final String CANCEL_PROTOCOL = "spen-region-2";
    private static final int CAST_POLL_MS = 75, MAX_CAST_BYTES = 8 * 1024 * 1024;
    enum State { OFFLINE, CONNECTING, ONLINE }
    final Handler main = new Handler(Looper.getMainLooper());
    private final ArrayList<Runnable> listeners = new ArrayList<>();
    private final ArrayList<Runnable> frameListeners = new ArrayList<>();
    volatile State state = State.OFFLINE;
    volatile boolean screen;
    volatile boolean writing;
    volatile boolean castSupported;
    volatile CastFrame castFrame = CastFrame.EMPTY;
    volatile String status = "Local canvas";
    volatile Endpoint endpoint;
    private int generation;
    private int castEpoch;
    private boolean frameNoticePending;
    private Socket socket;
    private Frame pending;
    private final ArrayDeque<Frame> annotations = new ArrayDeque<>();
    private boolean cancelSupported;
    private int control;

    void listen(Runnable listener) { listeners.add(listener); }
    void unlisten(Runnable listener) { listeners.remove(listener); }
    void listenFrames(Runnable listener) { frameListeners.add(listener); }
    void unlistenFrames(Runnable listener) { frameListeners.remove(listener); }

    private void changed() {
        main.post(() -> {
            for (Runnable listener : new ArrayList<>(listeners)) listener.run();
        });
    }

    private void frameChanged() {
        if (frameNoticePending) return;
        frameNoticePending = true;
        main.post(() -> {
            synchronized (Link.this) { frameNoticePending = false; }
            for (Runnable listener : new ArrayList<>(frameListeners)) listener.run();
        });
    }

    private void resetCast() {
        castEpoch++;
        castFrame = CastFrame.EMPTY;
        frameChanged();
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
        writing = false;
        castSupported = false;
        cancelSupported = false;
        resetCast();
        dropPending();
        annotations.clear();
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
            writing = false;
            resetCast();
            dropPending();
            annotations.clear();
            control = enabled ? 'M' : 'B';
            notifyAll();
        }
        if (state == State.ONLINE) {
            status = message == null ? "Paired · " + endpoint.host : message;
        }
        changed();
        return true;
    }

    synchronized boolean setWriting(boolean enabled) {
        if (enabled && (state != State.ONLINE || screen)) return false;
        if (writing != enabled) {
            writing = enabled;
            resetCast();
            dropPending();
            annotations.clear();
            control = enabled ? (castSupported ? 'V' : 'W') : 'B';
            notifyAll();
        }
        if (state == State.ONLINE) status = enabled ? "Writing on PC · " + endpoint.host
                : "Paired · " + endpoint.host;
        changed();
        return true;
    }

    synchronized boolean ready(boolean forScreen) {
        return state == State.ONLINE && screen == forScreen && !writing && pending == null;
    }

    synchronized boolean readyForWriting() {
        return state == State.ONLINE && writing && !screen;
    }

    boolean casting() { return writing && castSupported; }

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

    synchronized boolean offerAnnotation(byte[] data, int revision) {
        if (!readyForWriting()) return false;
        char action = (char) data[0];
        boolean terminal = action == 'E' || action == 'X';
        if (action == 'X' && !cancelSupported) data = new byte[]{'E'};
        if (casting()) {
            if (!terminal && (castFrame.status != CastFrame.READY || castFrame.revision != revision)) return false;
            data = ByteBuffer.allocate(4 + data.length).putInt(revision).put(data).array();
        }
        if (annotations.size() >= (terminal ? 512 : 511)) {
            if (action == 'M') return false;
            Frame move = null;
            for (Frame annotation : annotations) {
                if (annotation.move) { move = annotation; break; }
            }
            if (move == null) return false;
            annotations.remove(move);
        }
        annotations.addLast(new Frame('D', data, action == 'M'));
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
                    chain[0].checkValidity();
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
                tls.setEnabledProtocols(new String[]{"TLSv1.3"});
                SSLParameters parameters = tls.getSSLParameters();
                parameters.setApplicationProtocols(new String[]{CANCEL_PROTOCOL, CAST_PROTOCOL});
                tls.setSSLParameters(parameters);
                tls.startHandshake();
                String protocol = tls.getApplicationProtocol();
                boolean supportsCancel = CANCEL_PROTOCOL.equals(protocol);
                DataInputStream in = new DataInputStream(tls.getInputStream());
                DataOutputStream out = new DataOutputStream(new BufferedOutputStream(tls.getOutputStream()));
                out.write(("SPEN1 " + target.token + "\n").getBytes(StandardCharsets.US_ASCII));
                out.flush();
                int answer = in.read();
                if (answer != '+') {
                    error = answer == '*' ? "Another phone is paired with this PC." : "Pairing expired. Scan the PC again.";
                    return;
                }
                Endpoint paired = target;
                if (supportsCancel) {
                    byte[] data = new byte[32];
                    in.readFully(data);
                    String token = new String(data, StandardCharsets.US_ASCII);
                    if (!token.matches("[0-9a-f]{32}")) throw new IOException("Invalid reconnect token");
                    paired = target.withToken(token);
                }
                synchronized (this) {
                    if (generation != session) return;
                    castSupported = supportsCancel || CAST_PROTOCOL.equals(protocol);
                    cancelSupported = supportsCancel;
                    endpoint = paired;
                    state = State.ONLINE;
                    status = "Paired · " + target.host;
                    control = 'B';
                    changed();
                }
                long nextPoll = 0;
                ArrayList<Frame> batch = new ArrayList<>(32);
                while (true) {
                    Frame frame = null;
                    int kind, epoch;
                    synchronized (this) {
                        if (session == generation && control == 0 && pending == null && annotations.isEmpty()) {
                            long delay = casting() ? Math.max(0, nextPoll - SystemClock.uptimeMillis()) : 2000;
                            if (delay > 0) wait(delay);
                        }
                        if (session != generation) return;
                        epoch = castEpoch;
                        if (control != 0) {
                            kind = control;
                            control = 0;
                            nextPoll = 0;
                        } else if (casting() && SystemClock.uptimeMillis() >= nextPoll) {
                            kind = 'F';
                            nextPoll = SystemClock.uptimeMillis() + CAST_POLL_MS;
                        } else if (!annotations.isEmpty()) {
                            kind = 'D';
                            int count = Math.min(32, annotations.size());
                            for (int i = 0; i < count; i++) batch.add(annotations.removeFirst());
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
                        if (batch.isEmpty()) writeFrame(out, kind, frame);
                        else for (Frame event : batch) writeFrame(out, event.kind, event);
                        out.flush();
                        for (int i = 0; i < Math.max(1, batch.size()); i++) {
                            if (in.read() != '+') throw new IOException("PC disconnected");
                        }
                        if (kind == 'F') readCastFrame(in, session, epoch);
                    } finally {
                        batch.clear();
                        main.removeCallbacks(deadline);
                        if (frame != null && frame.bitmap != null) frame.bitmap.recycle();
                    }
                }
            }
        } catch (javax.net.ssl.SSLHandshakeException e) {
            error = "PC identity expired or changed. Restart the receiver and pair again.";
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

    private static void writeFrame(DataOutputStream out, int kind, Frame frame) throws IOException {
        out.writeByte(kind);
        if (frame == null) out.writeInt(0);
        else frame.write(out);
    }

    private void readCastFrame(DataInputStream in, int session, int epoch) throws IOException {
        int status = in.readUnsignedByte(), revision = in.readInt(), length = in.readInt();
        if (status > CastFrame.PAUSED || length < 0 || length > MAX_CAST_BYTES
                || (status != CastFrame.READY && length != 0)) throw new IOException("Invalid PC preview");
        Bitmap bitmap = null;
        if (length > 0) {
            byte[] jpeg = new byte[length];
            in.readFully(jpeg);
            BitmapFactory.Options options = new BitmapFactory.Options();
            options.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(jpeg, 0, length, options);
            if (options.outWidth < 1 || options.outHeight < 1 || options.outWidth > 2048 || options.outHeight > 2048)
                throw new IOException("Invalid PC preview dimensions");
            bitmap = BitmapFactory.decodeByteArray(jpeg, 0, length);
            if (bitmap == null) throw new IOException("Could not decode PC preview");
        }
        synchronized (this) {
            if (session != generation || epoch != castEpoch) return;
            if (bitmap == null && castFrame.status == status && castFrame.revision == revision) return;
            if (status == CastFrame.WAIT) bitmap = castFrame.bitmap;
            castFrame = new CastFrame(status, revision, bitmap);
            frameChanged();
        }
    }

    static final class CastFrame {
        static final int SELECT = 0, WAIT = 1, ERROR = 2, READY = 3, PAUSED = 4;
        static final CastFrame EMPTY = new CastFrame(SELECT, 0, null);
        final int status, revision;
        final Bitmap bitmap;

        CastFrame(int status, int revision, Bitmap bitmap) {
            this.status = status;
            this.revision = revision;
            this.bitmap = bitmap;
        }
    }

    private static final class Frame {
        final char kind;
        final Bitmap bitmap;
        final byte[] data;
        final int left, top;
        final boolean move;
        Frame(char kind, Bitmap bitmap, int left, int top) {
            this.kind = kind;
            this.bitmap = bitmap;
            this.data = null;
            this.move = false;
            this.left = left;
            this.top = top;
        }

        Frame(char kind, byte[] data, boolean move) {
            this.kind = kind;
            this.bitmap = null;
            this.data = data;
            this.move = move;
            this.left = this.top = 0;
        }

        void write(DataOutputStream out) throws IOException {
            if (data != null) {
                out.writeInt(data.length);
                out.write(data);
                return;
            }
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
