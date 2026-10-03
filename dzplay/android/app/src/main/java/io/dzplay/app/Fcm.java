package io.dzplay.app;

import android.content.Context;

/**
 * Firebase Cloud Messaging is optional: the classes in src/fcm are compiled only when the
 * build has google-services.json. Without it the app works the same, it just cannot ring
 * while it is closed (calls ring whenever the app is open).
 */
final class Fcm {
    private Fcm() {
    }

    static void start(Context context) {
        if (!BuildConfig.FCM) {
            return;
        }
        try {
            Class.forName("io.dzplay.app.FcmBootstrap").getMethod("start", Context.class).invoke(null, context);
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // Firebase not available on this phone (no Google services): in-app ringing still works
        }
    }
}
