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

    Endpoint withToken(String token) { return new Endpoint(host, port, token, pin); }

    static Endpoint parse(String link) {
        try {
            URI uri = new URI(link.trim());
            String[] parts = uri.getRawPath().split("/", -1);
            if (!"spenmirror".equals(uri.getScheme()) || uri.getHost() == null
                    || !validHost(uri.getHost())
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

    private static boolean validHost(String host) {
        if (!host.matches("[0-9]{1,3}(\\.[0-9]{1,3}){3}")) return false;
        String[] parts = host.split("\\.");
        int first = Integer.parseInt(parts[0]);
        if (first == 0 || first == 127 || first >= 224) return false;
        for (String part : parts) {
            if (Integer.parseInt(part) > 255 || (part.length() > 1 && part.charAt(0) == '0')) return false;
        }
        return true;
    }
}
