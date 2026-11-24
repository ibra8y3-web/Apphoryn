package com.apphoryn.store;

import android.content.res.AssetManager;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

final class LocalAssetServer implements Closeable {
    static final int PORT = 8765;
    private final AssetManager assets;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private ServerSocket server;

    LocalAssetServer(AssetManager assets) { this.assets = assets; }

    String start() throws IOException {
        server = new ServerSocket(PORT, 24, InetAddress.getByName("127.0.0.1"));
        running.set(true);
        Thread acceptThread = new Thread(() -> {
            while (running.get()) {
                try {
                    Socket client = server.accept();
                    workers.execute(() -> serve(client));
                } catch (IOException error) {
                    if (running.get()) android.util.Log.e("Apphoryn", "Local asset server failed", error);
                }
            }
        }, "apphoryn-local-http");
        acceptThread.setDaemon(true);
        acceptThread.start();
        return "http://127.0.0.1:" + server.getLocalPort() + "/index.html";
    }

    private void serve(Socket socket) {
        try (Socket client = socket) {
            client.setSoTimeout(8000);
            BufferedReader reader = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.US_ASCII));
            String requestLine = reader.readLine();
            if (requestLine == null || requestLine.length() > 4096) {
                write(client.getOutputStream(), 400, "Bad Request", "text/plain", new byte[0]); return;
            }
            String[] parts = requestLine.split(" ");
            if (parts.length != 3 || !("GET".equals(parts[0]) || "HEAD".equals(parts[0]))) {
                drainHeaders(reader);
                write(client.getOutputStream(), 405, "Method Not Allowed", "text/plain", new byte[0]); return;
            }
            drainHeaders(reader);
            String path = URLDecoder.decode(parts[1].split("\\?", 2)[0], "UTF-8").replace('\\', '/');
            if (path.isEmpty() || "/".equals(path)) path = "/index.html";
            for (String segment : path.split("/")) {
                if ("..".equals(segment) || ".".equals(segment)) {
                    write(client.getOutputStream(), 400, "Bad Request", "text/plain", new byte[0]); return;
                }
            }
            String assetPath = path.startsWith("/") ? path.substring(1) : path;
            byte[] body;
            try (InputStream input = assets.open(assetPath, AssetManager.ACCESS_STREAMING);
                 ByteArrayOutputStream buffer = new ByteArrayOutputStream()) {
                byte[] chunk = new byte[16384]; int count;
                while ((count = input.read(chunk)) >= 0) buffer.write(chunk, 0, count);
                body = buffer.toByteArray();
            } catch (IOException missing) {
                write(client.getOutputStream(), 404, "Not Found", "text/plain", "Not found".getBytes(StandardCharsets.UTF_8)); return;
            }
            write(client.getOutputStream(), 200, "OK", contentType(assetPath),
                "HEAD".equals(parts[0]) ? new byte[0] : body, body.length);
        } catch (IOException error) {
            android.util.Log.w("Apphoryn", "Local asset request failed", error);
        }
    }

    private static void drainHeaders(BufferedReader reader) throws IOException {
        String line; int count = 0;
        while ((line = reader.readLine()) != null && !line.isEmpty() && count++ < 64) {
            if (line.length() > 8192) throw new IOException("HTTP header too large");
        }
    }

    private static String contentType(String path) {
        String name = path.toLowerCase(Locale.ROOT);
        if (name.endsWith(".html")) return "text/html; charset=utf-8";
        if (name.endsWith(".js")) return "text/javascript; charset=utf-8";
        if (name.endsWith(".css")) return "text/css; charset=utf-8";
        if (name.endsWith(".json") || name.endsWith(".webmanifest")) return "application/json; charset=utf-8";
        if (name.endsWith(".svg")) return "image/svg+xml";
        if (name.endsWith(".png")) return "image/png";
        if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
        if (name.endsWith(".ico")) return "image/x-icon";
        return "application/octet-stream";
    }

    private static void write(OutputStream out, int status, String reason, String type, byte[] body) throws IOException {
        write(out, status, reason, type, body, body.length);
    }
    private static void write(OutputStream out, int status, String reason, String type, byte[] body, int length) throws IOException {
        String headers = "HTTP/1.1 " + status + " " + reason + "\r\n"
            + "Content-Type: " + type + "\r\nContent-Length: " + length
            + "\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff"
            + "\r\nConnection: close\r\n\r\n";
        out.write(headers.getBytes(StandardCharsets.US_ASCII)); out.write(body); out.flush();
    }

    @Override public void close() {
        running.set(false);
        try { if (server != null) server.close(); } catch (IOException ignored) { }
        workers.shutdownNow();
    }
}
