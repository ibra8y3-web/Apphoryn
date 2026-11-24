package com.apphoryn.store;

import android.app.Activity;
import android.app.PendingIntent;
import android.content.pm.PackageInstaller;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final long MAX_INSTALL_BYTES = 512L * 1024L * 1024L;
    private final ExecutorService installExecutor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private WebView webView;
    private LocalAssetServer assetServer;
    private int localPort = -1;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        setContentView(webView);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setSupportZoom(false);
        webView.addJavascriptInterface(this, "ApphorynNative");
        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("127.0.0.1".equals(uri.getHost()) && uri.getPort() == localPort) return false;
                if ("https".equalsIgnoreCase(uri.getScheme())) {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                }
                return true;
            }
        });

        try {
            assetServer = new LocalAssetServer(getAssets());
            String startUrl = assetServer.start();
            localPort = Uri.parse(startUrl).getPort();
            webView.loadUrl(startUrl);
        } catch (Exception error) {
            Toast.makeText(this, "Apphoryn could not start: " + error.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @JavascriptInterface
    public void installArtifact(String apiBase, String artifactId, String filename,
                                String platform, String packageType, String expectedSha256,
                                String downloadUrl) {
        try {
            validateRequest(artifactId, filename, platform, packageType, expectedSha256, downloadUrl);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    && !getPackageManager().canRequestPackageInstalls()) {
                Intent settingsIntent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + getPackageName()));
                startActivity(settingsIntent);
                showToast("Allow Apphoryn to install verified APK/XAPK packages, then press Install again.");
                return;
            }
        } catch (Exception error) {
            showToast(error.getMessage());
            return;
        }

        installExecutor.execute(() -> {
            try {
                File packageFile = downloadAndVerify(downloadUrl, expectedSha256, filename);
                if (filename.toLowerCase(Locale.ROOT).endsWith(".xapk")) {
                    installXapkBundle(packageFile);
                } else {
                    mainHandler.post(() -> launchAndroidInstaller(packageFile));
                }
            } catch (Exception error) {
                showToast("Apphoryn could not prepare this Android package: " + error.getMessage());
            }
        });
        showToast("Downloading the verified package from Apphoryn storage…");
    }

    private static URI validateApphorynReleaseUrl(String value) throws Exception {
        URI uri = URI.create(value);
        String expectedPath = "/ibra8y3-web/Apphoryn/releases/download/";
        if (!"https".equalsIgnoreCase(uri.getScheme())
                || !"github.com".equalsIgnoreCase(uri.getHost())
                || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null
                || uri.getPath() == null || !uri.getPath().startsWith(expectedPath)) {
            throw new SecurityException("Only Apphoryn-owned HTTPS release packages can be installed.");
        }
        return uri;
    }

    private static boolean isApprovedReleaseHost(URI uri) {
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getUserInfo() != null
                || uri.getFragment() != null) return false;
        String host = String.valueOf(uri.getHost()).toLowerCase(Locale.ROOT);
        return (uri.getPort() == -1 || uri.getPort() == 443)
                && (host.equals("github.com")
                || host.equals("objects.githubusercontent.com")
                || host.equals("release-assets.githubusercontent.com")
                || host.equals("github-releases.githubusercontent.com"));
    }

    private static void validateRequest(String artifactId, String filename, String platform,
                                        String packageType, String expectedSha256, String downloadUrl) throws Exception {
        if (!artifactId.matches("[A-Za-z0-9._-]{1,128}")) {
            throw new IllegalArgumentException("Invalid artifact identifier.");
        }
        String lowerFilename = filename.toLowerCase(Locale.ROOT);
        if (!"android".equalsIgnoreCase(platform)
                || !(lowerFilename.endsWith(".apk") || lowerFilename.endsWith(".xapk"))) {
            throw new IllegalArgumentException("This Android client installs APK and signed XAPK split bundles.");
        }
        if (!expectedSha256.matches("(?i)[0-9a-f]{64}")) {
            throw new IllegalArgumentException("Missing or invalid SHA-256 digest.");
        }
        if (filename.contains("/") || filename.indexOf((char) 92) >= 0) {
            throw new IllegalArgumentException("Invalid package filename.");
        }
        validateApphorynReleaseUrl(downloadUrl);
    }

    private HttpURLConnection openApprovedReleaseConnection(String downloadUrl) throws Exception {
        URI current = validateApphorynReleaseUrl(downloadUrl);
        for (int redirects = 0; redirects <= 5; redirects++) {
            if (!isApprovedReleaseHost(current)) {
                throw new SecurityException("Release download redirected to an unapproved host.");
            }
            HttpURLConnection connection = (HttpURLConnection) new URL(current.toString()).openConnection();
            connection.setConnectTimeout(20000);
            connection.setReadTimeout(60000);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("Accept", "application/octet-stream");
            connection.setRequestProperty("User-Agent", "Apphoryn-Android/1.0");
            int status = connection.getResponseCode();
            if (status >= 300 && status < 400) {
                String location = connection.getHeaderField("Location");
                connection.disconnect();
                if (location == null || redirects == 5) {
                    throw new IllegalStateException("Apphoryn release download exceeded its redirect limit.");
                }
                current = current.resolve(location);
                continue;
            }
            if (status != 200) {
                connection.disconnect();
                throw new IllegalStateException("Apphoryn release returned HTTP " + status + ".");
            }
            return connection;
        }
        throw new IllegalStateException("Apphoryn release download exceeded its redirect limit.");
    }

    private File downloadAndVerify(String downloadUrl, String expectedSha256, String filename) throws Exception {
        HttpURLConnection connection = openApprovedReleaseConnection(downloadUrl);
        try {
            String declaredHeader = connection.getHeaderField("Content-Length");
            if (declaredHeader != null && !declaredHeader.isEmpty()) {
                long declared;
                try {
                    declared = Long.parseLong(declaredHeader);
                } catch (NumberFormatException error) {
                    throw new IllegalStateException("Apphoryn release returned an invalid content length.");
                }
                if (declared > MAX_INSTALL_BYTES) {
                    throw new IllegalStateException("APK exceeds the Android client size limit.");
                }
            }

            File directory = new File(getCacheDir(), "installers");
            if (!directory.exists() && !directory.mkdirs()) {
                throw new IllegalStateException("Could not create private installer cache.");
            }
            String suffix = filename.toLowerCase(Locale.ROOT).endsWith(".xapk") ? ".xapk" : ".apk";
            File destination = new File(directory, UUID.randomUUID() + suffix);
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            long total = 0;
            try (InputStream input = connection.getInputStream();
                 FileOutputStream output = new FileOutputStream(destination)) {
                byte[] buffer = new byte[65536];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    total += count;
                    if (total > MAX_INSTALL_BYTES) throw new IllegalStateException("APK exceeded the Android client size limit.");
                    digest.update(buffer, 0, count);
                    output.write(buffer, 0, count);
                }
                output.getFD().sync();
            } catch (Exception error) {
                destination.delete();
                throw error;
            }

            StringBuilder actual = new StringBuilder();
            for (byte value : digest.digest()) actual.append(String.format(Locale.ROOT, "%02x", value));
            if (!actual.toString().equalsIgnoreCase(expectedSha256)) {
                destination.delete();
                throw new SecurityException("Downloaded APK SHA-256 does not match the Apphoryn catalogue.");
            }
            return destination;
        } finally {
            connection.disconnect();
        }
    }

    private void installXapkBundle(File xapkFile) throws Exception {
        File extractionRoot = new File(getCacheDir(), "installers/" + UUID.randomUUID());
        if (!extractionRoot.mkdirs()) {
            throw new IllegalStateException("Could not create a private XAPK extraction directory.");
        }
        List<File> splitFiles = new ArrayList<>();
        long totalExpanded = 0;
        boolean baseFound = false;
        boolean obbFound = false;
        Set<String> outputNames = new HashSet<>();
        try (ZipFile archive = new ZipFile(xapkFile)) {
            Enumeration<? extends ZipEntry> entries = archive.entries();
            int apkCount = 0;
            while (entries.hasMoreElements()) {
                ZipEntry entry = entries.nextElement();
                if (entry.isDirectory()) continue;
                String normalized = entry.getName().replace('\\', '/');
                if (normalized.startsWith("/") || normalized.matches("^[A-Za-z]:.*")) {
                    throw new SecurityException("XAPK contains an absolute file path.");
                }
                for (String segment : normalized.split("/")) {
                    if ("..".equals(segment) || ".".equals(segment)) {
                        throw new SecurityException("XAPK contains a path traversal entry.");
                    }
                }
                String lower = normalized.toLowerCase(Locale.ROOT);
                if (lower.endsWith(".obb")) {
                    obbFound = true;
                    continue;
                }
                if (!lower.endsWith(".apk")) continue;
                apkCount++;
                if (apkCount > 16) throw new IllegalStateException("XAPK contains too many APK split files.");
                String name = normalized.substring(normalized.lastIndexOf('/') + 1);
                if (!name.matches("(?i)[A-Za-z0-9._-]+\\.apk")) {
                    throw new SecurityException("XAPK contains an unsafe APK split filename.");
                }
                if (!outputNames.add(name.toLowerCase(Locale.ROOT))) {
                    throw new SecurityException("XAPK contains duplicate APK split filenames.");
                }
                if ("base.apk".equalsIgnoreCase(name)) baseFound = true;
                File outputFile = new File(extractionRoot, name);
                long partBytes = 0;
                try (InputStream input = archive.getInputStream(entry);
                     FileOutputStream output = new FileOutputStream(outputFile)) {
                    byte[] buffer = new byte[65536];
                    int count;
                    while ((count = input.read(buffer)) != -1) {
                        partBytes += count;
                        totalExpanded += count;
                        if (partBytes > MAX_INSTALL_BYTES || totalExpanded > MAX_INSTALL_BYTES) {
                            throw new IllegalStateException("XAPK expanded data exceeds the installer size limit.");
                        }
                        output.write(buffer, 0, count);
                    }
                    output.getFD().sync();
                }
                splitFiles.add(outputFile);
            }
            if (obbFound) {
                throw new IllegalStateException("This XAPK includes OBB expansion data. Apphoryn currently installs APK split bundles without OBB payloads.");
            }
            if (!baseFound || splitFiles.isEmpty()) {
                throw new IllegalStateException("XAPK does not contain a base.apk file.");
            }
        } catch (Exception error) {
            deleteTree(extractionRoot);
            xapkFile.delete();
            throw error;
        }

        splitFiles.sort((left, right) -> {
            if ("base.apk".equalsIgnoreCase(left.getName())) return -1;
            if ("base.apk".equalsIgnoreCase(right.getName())) return 1;
            return left.getName().compareToIgnoreCase(right.getName());
        });

        PackageInstaller installer = getPackageManager().getPackageInstaller();
        int sessionId = -1;
        try {
            PackageInstaller.SessionParams params = new PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            sessionId = installer.createSession(params);
            try (PackageInstaller.Session session = installer.openSession(sessionId)) {
                for (File split : splitFiles) {
                    try (InputStream input = new java.io.FileInputStream(split);
                         java.io.OutputStream output = session.openWrite(split.getName(), 0, split.length())) {
                        byte[] buffer = new byte[65536];
                        int count;
                        while ((count = input.read(buffer)) != -1) output.write(buffer, 0, count);
                        session.fsync(output);
                    }
                }
                Intent statusIntent = new Intent(this, InstallStatusReceiver.class);
                statusIntent.setAction(InstallStatusReceiver.ACTION_INSTALL_STATUS);
                statusIntent.putExtra(InstallStatusReceiver.EXTRA_SESSION_ID, sessionId);
                int flags = PendingIntent.FLAG_UPDATE_CURRENT;
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) flags |= PendingIntent.FLAG_MUTABLE;
                PendingIntent statusPendingIntent = PendingIntent.getBroadcast(this, sessionId, statusIntent, flags);
                session.commit(statusPendingIntent.getIntentSender());
            }
            showToast("Android Package Installer is preparing the XAPK split bundle…");
        } catch (Exception error) {
            if (sessionId >= 0) {
                try { installer.abandonSession(sessionId); } catch (Exception ignored) { }
            }
            throw error;
        } finally {
            deleteTree(extractionRoot);
            xapkFile.delete();
        }
    }

    private static void deleteTree(File file) {
        if (file == null || !file.exists()) return;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) deleteTree(child);
        file.delete();
    }

    private void launchAndroidInstaller(File apk) {
        try {
            Uri uri = FileProvider.getUriForFile(this, getPackageName() + ".fileprovider", apk);
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(uri, "application/vnd.android.package-archive");
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(intent);
        } catch (Exception error) {
            showToast("Android could not open its package installer: " + error.getMessage());
        }
    }

    private void showToast(String message) {
        mainHandler.post(() -> Toast.makeText(this, message, Toast.LENGTH_LONG).show());
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.removeJavascriptInterface("ApphorynNative");
            webView.destroy();
        }
        if (assetServer != null) assetServer.close();
        installExecutor.shutdownNow();
        super.onDestroy();
    }
}
