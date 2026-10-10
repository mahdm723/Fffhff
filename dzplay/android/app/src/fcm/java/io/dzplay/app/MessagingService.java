package io.dzplay.app;

import android.content.Context;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

/**
 * Data-only pushes from the server (no name, number or text inside):
 *   {type: "message"} → "لديك رسالة جديدة على <appName>"
 * Anything else (such as "call" from a server older than V6) is ignored.
 */
public class MessagingService extends FirebaseMessagingService {

    static void saveToken(Context c, String token) {
        c.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE).edit().putString("fcm_token", token).apply();
    }

    @Override
    public void onNewToken(String token) {
        saveToken(this, token); // sent to the server the next time the app opens
    }

    @Override
    public void onMessageReceived(RemoteMessage message) {
        if ("message".equals(message.getData().get("type"))) {
            Notifications.showMessage(this);
        }
    }
}
