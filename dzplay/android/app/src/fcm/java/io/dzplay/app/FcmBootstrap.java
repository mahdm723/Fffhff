package io.dzplay.app;

import android.content.Context;

import com.google.firebase.messaging.FirebaseMessaging;

/** Fetches this phone's Firebase token; the web app registers it with the server (/api/push/fcm). */
public final class FcmBootstrap {
    private FcmBootstrap() {
    }

    public static void start(Context context) {
        FirebaseMessaging.getInstance().getToken().addOnSuccessListener(token -> MessagingService.saveToken(context, token));
    }
}
