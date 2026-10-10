package io.dzplay.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

/**
 * DALTA.BIT in its own full-screen WebView (the name comes from gradle.properties: appName).
 *
 * - Only https://APP_HOST pages are shown inside the app; any other link opens
 *   in the phone's browser (the in-app page can never be swapped for another site).
 * - No dependency on Chrome or any browser app (works on phones without Google
 *   services and with "dual apps"/cloned browsers).
 * - The session cookie lives in the app's own private WebView storage.
 * - V6 (3.0.0): no calls any more, so no camera, microphone, audio routing or full-screen
 *   notification; any camera / microphone request from a page is refused.
 * - With Firebase (optional build), a new-message notification arrives while the app is closed.
 */
public class MainActivity extends Activity {

    static final String HOST = BuildConfig.APP_HOST;
    static final String HOME = "https://" + HOST + "/";
    static final String PREFS = "dzplay";
    private static final int REQ_NOTIFY = 42;
    private static final int REQ_FILE = 43;

    private WebView web;
    private FrameLayout root;
    private int topInsetPx = 0;
    private volatile boolean onOwnPage = false;
    private String theme = "system"; // light | dark | system, as chosen in «المظهر» (sent by the page)
    private ValueCallback<Uri[]> pendingFiles;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0C0E18"));
        root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
        setupInsets();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);          // the app is a JavaScript web app
        s.setDomStorageEnabled(true);          // local history cache + drafts
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        s.setMediaPlaybackRequiresUserGesture(true);  // nothing plays by itself
        s.setUserAgentString(s.getUserAgentString() + " DZPLAYApp/" + BuildConfig.VERSION_NAME);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(web, false);

        web.addJavascriptInterface(new Bridge(), "DZPLAYAndroid");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                request.deny(); // no camera / microphone in V6 (and never for another site)
            }

            // <input type="file"> (pictures for ideas, chats and the profile): the system
            // picker returns a content:// URI the page may read once; no storage permission needed.
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (!onOwnPage) {
                    return false;
                }
                if (pendingFiles != null) {
                    pendingFiles.onReceiveValue(null);
                }
                pendingFiles = callback;
                try {
                    Intent intent = params.createIntent();
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(Intent.createChooser(intent, null), REQ_FILE);
                    return true;
                } catch (ActivityNotFoundException e) {
                    pendingFiles = null;
                    return false;
                }
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isOwnSite(uri)) {
                    return false; // stay in the app
                }
                openOutside(uri);
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                onOwnPage = url != null && isOwnSite(Uri.parse(url));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();
                pushInsets();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showOffline();
                }
            }
        });
        // The APK on /download (app update) is handed to the system downloader / browser.
        web.setDownloadListener((url, userAgent, contentDisposition, mimetype, contentLength) -> openOutside(Uri.parse(url)));

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(startUrl(getIntent()));
        }
        Fcm.start(this);
        askNotificationsOnce();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Uri data = intent.getData();
        if (data != null && isOwnSite(data)) {
            web.loadUrl(data.toString());
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        Notifications.cancelMessage(this); // the app is open: the conversation shows the rest
    }

    @Override
    public void onBackPressed() {
        // The web app uses #/routes, so going back walks through its screens first.
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------ system bars + keyboard
    //
    // Android 15 (targetSdk 35) always draws edge-to-edge and then ignores adjustResize, so the
    // page could slide under the navigation buttons and the keyboard. From Android 11 on we do it
    // ourselves: the WebView is padded by the real navigation-bar / keyboard insets (the page
    // shrinks when the keyboard opens, like adjustResize) and the status-bar height is handed to
    // the page as --dz-safe-top. Older versions keep the classic layout (adjustResize works there).

    private boolean night() {
        if ("dark".equals(theme)) {
            return true;
        }
        if ("light".equals(theme)) {
            return false;
        }
        return (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
    }

    private void paintBars() {
        int bg = night() ? Color.parseColor("#0C0E18") : Color.parseColor("#F5F2EC");
        root.setBackgroundColor(bg);
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                int light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
                c.setSystemBarsAppearance(night() ? 0 : light, light);
            }
        }
    }

    private void setupInsets() {
        if (Build.VERSION.SDK_INT < 30) {
            return;
        }
        getWindow().setDecorFitsSystemWindows(false);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        paintBars();
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            android.graphics.Insets ime = insets.getInsets(WindowInsets.Type.ime());
            v.setPadding(bars.left, 0, bars.right, Math.max(bars.bottom, ime.bottom));
            topInsetPx = bars.top;
            pushInsets();
            return WindowInsets.CONSUMED;
        });
        root.requestApplyInsets();
    }

    private void pushInsets() {
        if (web == null || Build.VERSION.SDK_INT < 30) {
            return;
        }
        float density = getResources().getDisplayMetrics().density;
        int top = Math.round(topInsetPx / density);
        web.evaluateJavascript("document.documentElement.style.setProperty('--dz-safe-top','" + top + "px');"
                + "document.documentElement.style.setProperty('--dz-safe-bottom','0px');", null);
    }

    @Override
    public void onConfigurationChanged(Configuration newConfig) {
        super.onConfigurationChanged(newConfig);
        if (root != null && Build.VERSION.SDK_INT >= 30) {
            paintBars(); // dark / light theme switched
        }
    }

    static boolean isOwnSite(Uri uri) {
        return uri != null && "https".equals(uri.getScheme()) && HOST.equalsIgnoreCase(uri.getHost());
    }

    private String startUrl(Intent intent) {
        Uri data = intent != null ? intent.getData() : null;
        return isOwnSite(data) ? data.toString() : HOME;
    }

    private void openOutside(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException ignored) {
            // nothing can open it — stay on the current page
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE) {
            if (pendingFiles != null) {
                pendingFiles.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
                pendingFiles = null;
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private void askNotificationsOnce() {
        if (Build.VERSION.SDK_INT < 33 || !BuildConfig.FCM) {
            return;
        }
        SharedPreferences p = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (p.getBoolean("asked_notify", false)
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            return;
        }
        p.edit().putBoolean("asked_notify", true).apply();
        requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
    }

    private void showOffline() {
        String page = "<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
            + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            + "<style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
            + "justify-content:center;gap:14px;background:#0c0e18;color:#eef0f8;font-family:sans-serif;text-align:center;padding:24px}"
            + "b{font-size:30px}a{background:linear-gradient(135deg,#ff8a5c,#ff5f8a);color:#fff;padding:12px 26px;"
            + "border-radius:16px;text-decoration:none;font-weight:bold}p{color:#b4b9cc}</style></head><body>"
            + "<b>" + getString(R.string.appName) + "</b><p>تعذّر الاتصال. تحقق من الإنترنت ثم أعد المحاولة.</p>"
            + "<a href='" + HOME + "'>إعادة المحاولة</a></body></html>";
        onOwnPage = false;
        web.loadDataWithBaseURL(null, page, "text/html", "utf-8", null);
    }

    /** Small native helpers for the web app (window.DZPLAYAndroid, a stable internal name), active only on our own pages. */
    private class Bridge {
        @JavascriptInterface
        public void share(String text) {
            if (!onOwnPage) {
                return;
            }
            runOnUiThread(() -> {
                Intent send = new Intent(Intent.ACTION_SEND);
                send.setType("text/plain");
                send.putExtra(Intent.EXTRA_TEXT, text);
                startActivity(Intent.createChooser(send, getString(R.string.shareTitle, getString(R.string.appName))));
            });
        }

        /** «المظهر» in the app: the status / navigation bar icons follow the chosen theme, not only the phone's. */
        @JavascriptInterface
        public void setTheme(String mode) {
            if (!onOwnPage || mode == null || !mode.matches("light|dark|system")) {
                return;
            }
            runOnUiThread(() -> {
                theme = mode;
                if (root != null && Build.VERSION.SDK_INT >= 30) {
                    paintBars();
                }
            });
        }

        @JavascriptInterface
        public int versionCode() {
            return BuildConfig.VERSION_CODE;
        }

        @JavascriptInterface
        public String fcmToken() {
            if (!onOwnPage) {
                return "";
            }
            return getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("fcm_token", "");
        }

        /** While an ephemeral chat image is open: block screenshots / screen recording of the app window. */
        @JavascriptInterface
        public void setSecure(boolean on) {
            if (!onOwnPage) {
                return;
            }
            runOnUiThread(() -> {
                if (on) {
                    getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
                } else {
                    getWindow().clearFlags(WindowManager.LayoutParams.FLAG_SECURE);
                }
            });
        }
    }
}
