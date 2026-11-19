package ink.relay;

import java.net.URI;

final class Endpoint {
    final String host, token;
    final int port;
    final byte[] pin;

    private Endpoint(String host, int port, String token, byte[] pin) {
        this.host = host;
        this.port = port;
        this.token = token;
        this.pin = pin;
    }

    static Endpoint parse(String link) {
        try {
            URI uri = new URI(link.trim());
            String[] parts = uri.getRawPath().split("/", -1);
            if (!"spenmirror".equals(uri.getScheme()) || uri.getHost() == null
                    || !uri.getHost().matches("[0-9]+(\\.[0-9]+){3}")
                    || uri.getPort() < 1 || uri.getPort() > 65535 || uri.getUserInfo() != null
                    || uri.getRawQuery() != null || uri.getRawFragment() != null
                    || parts.length != 4 || !parts[1].equals("v1")
                    || !parts[2].matches("[0-9a-f]{32}") || !parts[3].matches("[0-9a-f]{64}")) {
                throw new IllegalArgumentException();
            }
            byte[] pin = new byte[32];
            for (int i = 0; i < pin.length; i++) {
                pin[i] = (byte) Integer.parseInt(parts[3].substring(i * 2, i * 2 + 2), 16);
            }
            return new Endpoint(uri.getHost(), uri.getPort(), parts[2], pin);
        } catch (Exception e) {
            throw new IllegalArgumentException("Scan or paste the pairing link from the PC window.");
        }
    }
}
