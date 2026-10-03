package io.dzplay.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.webkit.CookieManager;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** "رفض" on the ring notification: declines the call with the app's own session (no UI). */
public class DeclineReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        CallNotifications.cancelIncoming(context);
        String callId = intent.getStringExtra(MainActivity.EXTRA_CALL);
        if (callId == null || !callId.matches("[A-Za-z0-9_-]{1,32}")) {
            return;
        }
        String cookie = CookieManager.getInstance().getCookie(MainActivity.HOME);
        if (cookie == null) {
            return;
        }
        PendingResult pending = goAsync();
        new Thread(() -> {
            HttpURLConnection con = null;
            try {
                con = (HttpURLConnection) new URL(MainActivity.HOME + "api/calls/" + callId + "/decline").openConnection();
                con.setRequestMethod("POST");
                con.setConnectTimeout(8000);
                con.setReadTimeout(8000);
                con.setDoOutput(true);
                con.setRequestProperty("Cookie", cookie);
                con.setRequestProperty("Content-Type", "application/json");
                con.setRequestProperty("X-DZ-Requested", "1"); // the API's CSRF guard
                con.setRequestProperty("Origin", "https://" + MainActivity.HOST);
                try (OutputStream out = con.getOutputStream()) {
                    out.write("{}".getBytes(StandardCharsets.UTF_8));
                }
                con.getResponseCode();
            } catch (Exception ignored) {
                // offline: the call becomes "missed" after the ring timeout anyway
            } finally {
                if (con != null) {
                    con.disconnect();
                }
                pending.finish();
            }
        }).start();
    }
}
