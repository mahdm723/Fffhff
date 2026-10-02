package io.dzplay.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * DZPLAY in its own full-screen WebView.
 *
 * - Only https://APP_HOST pages are shown inside the app; any other link opens
 *   in the phone's browser (the in-app page can never be swapped for another site).
 * - No dependency on Chrome or any browser app (works on phones without Google
 *   services and with "dual apps"/cloned browsers).
 * - The session cookie lives in the app's own private WebView storage.
 */
public class MainActivity extends Activity {

    private static final String HOST = BuildConfig.APP_HOST;
    private static final String HOME = "https://" + HOST + "/";

    private WebView web;

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0C0E18"));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);          // the app is a JavaScript web app
        s.setDomStorageEnabled(true);          // local history cache + drafts
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        // Reels autoplay MUTED as you scroll (the web app never starts sound by itself; unmuting needs a tap).
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(s.getUserAgentString() + " DZPLAYApp/" + BuildConfig.VERSION_NAME);

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(web, false);

        web.addJavascriptInterface(new Bridge(), "DZPLAYAndroid");
        web.setWebChromeClient(new WebChromeClient());
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
            public void onPageFinished(WebView view, String url) {
                CookieManager.getInstance().flush();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showOffline();
                }
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(startUrl(getIntent()));
        }
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

    private static boolean isOwnSite(Uri uri) {
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

    private void showOffline() {
        String page = "<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
            + "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            + "<style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
            + "justify-content:center;gap:14px;background:#0c0e18;color:#eef0f8;font-family:sans-serif;text-align:center;padding:24px}"
            + "b{font-size:30px}a{background:linear-gradient(135deg,#ff8a5c,#ff5f8a);color:#fff;padding:12px 26px;"
            + "border-radius:16px;text-decoration:none;font-weight:bold}p{color:#b4b9cc}</style></head><body>"
            + "<b>dzplay</b><p>تعذّر الاتصال. تحقق من الإنترنت ثم أعد المحاولة.</p>"
            + "<a href='" + HOME + "'>إعادة المحاولة</a></body></html>";
        web.loadDataWithBaseURL(null, page, "text/html", "utf-8", null);
    }

    /** Small native helpers for the web app (window.DZPLAYAndroid). */
    private class Bridge {
        @JavascriptInterface
        public void share(String text) {
            runOnUiThread(() -> {
                Intent send = new Intent(Intent.ACTION_SEND);
                send.setType("text/plain");
                send.putExtra(Intent.EXTRA_TEXT, text);
                startActivity(Intent.createChooser(send, getString(R.string.shareTitle)));
            });
        }
    }
}
