package com.apphoryn.store;

import android.app.Activity;
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
                showToast("Allow Apphoryn to install verified APK files, then press Install again.");
                return;
            }
        } catch (Exception error) {
            showToast(error.getMessage());
            return;
        }

        installExecutor.execute(() -> {
            try {
                File apk = downloadAndVerify(downloadUrl, expectedSha256);
                mainHandler.post(() -> launchAndroidInstaller(apk));
            } catch (Exception error) {
                showToast("Apphoryn could not prepare this APK: " + error.getMessage());
            }
        });
        showToast("Downloading the verified APK from Apphoryn storage…");
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
        return host.equals("github.com")
                || host.equals("objects.githubusercontent.com")
                || host.equals("release-assets.githubusercontent.com")
                || host.equals("github-releases.githubusercontent.com");
    }

    private static void validateRequest(String artifactId, String filename, String platform,
                                        String packageType, String expectedSha256, String downloadUrl) throws Exception {
        if (!artifactId.matches("[A-Za-z0-9._-]{1,128}")) {
            throw new IllegalArgumentException("Invalid artifact identifier.");
        }
        if (!"android".equalsIgnoreCase(platform)
                || !"apk".equalsIgnoreCase(packageType)
                || !filename.toLowerCase(Locale.ROOT).endsWith(".apk")) {
            throw new IllegalArgumentException("This Android client currently installs verified APK files only.");
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

    private File downloadAndVerify(String downloadUrl, String expectedSha256) throws Exception {
        HttpURLConnection connection = openApprovedReleaseConnection(downloadUrl);
        try {
            long declared = connection.getContentLengthLong();
            if (declared > MAX_INSTALL_BYTES) throw new IllegalStateException("APK exceeds the Android client size limit.");

            File directory = new File(getCacheDir(), "installers");
            if (!directory.exists() && !directory.mkdirs()) {
                throw new IllegalStateException("Could not create private installer cache.");
            }
            File destination = new File(directory, UUID.randomUUID() + ".apk");
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
