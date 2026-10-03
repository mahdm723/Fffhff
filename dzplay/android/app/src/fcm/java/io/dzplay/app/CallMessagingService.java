package io.dzplay.app;

import android.content.Context;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.util.Map;

/**
 * Data-only pushes from the DZPLAY server (no name, number or text inside):
 *   {type: "call", call_id}  → full-screen incoming-call notification
 *   {type: "message"}        → "لديك رسالة جديدة على DZPLAY"
 */
public class CallMessagingService extends FirebaseMessagingService {
    static final int RING_SECONDS = 35;

    static void saveToken(Context c, String token) {
        c.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE).edit().putString("fcm_token", token).apply();
    }

    @Override
    public void onNewToken(String token) {
        saveToken(this, token); // sent to the server the next time the app opens
    }

    @Override
    public void onMessageReceived(RemoteMessage message) {
        Map<String, String> data = message.getData();
        String type = data.get("type");
        if ("call".equals(type)) {
            String id = data.get("call_id");
            if (id != null && id.matches("[A-Za-z0-9_-]{1,32}")) {
                CallNotifications.showIncoming(this, id, RING_SECONDS);
            }
        } else if ("message".equals(type)) {
            CallNotifications.showMessage(this);
        }
    }
}
