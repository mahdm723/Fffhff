package io.dzplay.app;

import android.content.Context;

/**
 * Firebase Cloud Messaging is optional: the classes in src/fcm are compiled only when the
 * build has google-services.json. Without it the app works the same, it just shows no
 * new-message notification while it is closed.
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
            // Firebase not available on this phone (no Google services): the app works the same
        }
    }
}
