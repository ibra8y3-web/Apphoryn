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
                                String platform, String packageType, String expectedSha256) {
        try {
            validateRequest(apiBase, artifactId, filename, platform, expectedSha256);
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
                File apk = downloadAndVerify(apiBase, artifactId, expectedSha256);
                mainHandler.post(() -> launchAndroidInstaller(apk));
            } catch (Exception error) {
                showToast("Apphoryn could not prepare this APK: " + error.getMessage());
            }
        });
        showToast("Downloading the verified APK inside Apphoryn…");
    }

    private static void validateRequest(String apiBase, String artifactId, String filename,
                                        String platform, String expectedSha256) throws Exception {
        URI base = URI.create(apiBase);
        if (!"https".equalsIgnoreCase(base.getScheme()) || base.getHost() == null
                || base.getUserInfo() != null || base.getQuery() != null || base.getFragment() != null) {
            throw new IllegalArgumentException("The Apphoryn API must use a valid HTTPS address.");
        }
        if (!artifactId.matches("[A-Za-z0-9._-]{1,128}")) {
            throw new IllegalArgumentException("Invalid artifact identifier.");
        }
        if (!"android".equalsIgnoreCase(platform) || !filename.toLowerCase(Locale.ROOT).endsWith(".apk")) {
            throw new IllegalArgumentException("This Android client currently installs verified APK files only.");
        }
        if (!expectedSha256.matches("(?i)[0-9a-f]{64}")) {
            throw new IllegalArgumentException("Missing or invalid SHA-256 digest.");
        }
        if (filename.contains("/") || filename.indexOf((char) 92) >= 0) {
            throw new IllegalArgumentException("Invalid package filename.");
        }
    }

    private File downloadAndVerify(String apiBase, String artifactId, String expectedSha256) throws Exception {
        String root = apiBase.endsWith("/") ? apiBase : apiBase + "/";
        URI base = URI.create(root);
        URI endpoint = base.resolve("api/v1/downloads/" + Uri.encode(artifactId));
        if (!base.getHost().equals(endpoint.getHost()) || !"https".equalsIgnoreCase(endpoint.getScheme())) {
            throw new IllegalArgumentException("The package URL is outside the configured Apphoryn API.");
        }

        HttpURLConnection connection = (HttpURLConnection) new URL(endpoint.toString()).openConnection();
        connection.setConnectTimeout(20000);
        connection.setReadTimeout(60000);
        connection.setInstanceFollowRedirects(false);
        connection.setRequestProperty("Accept", "application/octet-stream");
        try {
            int status = connection.getResponseCode();
            if (status != 200) throw new IllegalStateException("Apphoryn download API returned HTTP " + status + ".");
            int declared = connection.getContentLength();
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
