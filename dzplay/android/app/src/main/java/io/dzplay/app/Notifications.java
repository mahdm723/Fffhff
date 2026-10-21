package io.dzplay.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;

/**
 * The new-message notification (Firebase builds only). It never shows who wrote or what:
 * only "لديك رسالة جديدة على <appName>" — the app shows the rest after it opens.
 * V6 phase 14 (3.2.0): the server sends that line in the account's language (title / body); without them (an older
 * server) the phone's language is used (values-fr, values-en).
 * V6 (3.0.0): calls were removed, so the old "المكالمات الواردة" channel is deleted on upgrade.
 */
final class Notifications {
    static final String MESSAGE_CHANNEL = "dz_messages";
    static final String OLD_CALL_CHANNEL = "dz_calls";
    static final int MESSAGE_ID = 4200;

    private Notifications() {
    }

    private static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        nm.deleteNotificationChannel(OLD_CALL_CHANNEL);
        nm.createNotificationChannel(new NotificationChannel(MESSAGE_CHANNEL, c.getString(R.string.messageChannel), NotificationManager.IMPORTANCE_HIGH));
    }

    static void showMessage(Context c) {
        showMessage(c, null, null);
    }

    static void showMessage(Context c, String title, String body) {
        channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        Intent open = new Intent(c, MainActivity.class)
                .setData(Uri.parse(MainActivity.HOME))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent tap = PendingIntent.getActivity(c, 5, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, MESSAGE_CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(clean(title, c.getString(R.string.appName)))
                .setContentText(clean(body, c.getString(R.string.newMessage, c.getString(R.string.appName))))
                .setAutoCancel(true)
                .setContentIntent(tap);
        nm.notify(MESSAGE_ID, b.build());
    }

    /** A short plain line from the server, else the app's own text. */
    private static String clean(String text, String fallback) {
        if (text == null) {
            return fallback;
        }
        String t = text.trim();
        return t.isEmpty() || t.length() > 120 ? fallback : t;
    }

    static void cancelMessage(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.cancel(MESSAGE_ID);
        }
        channels(c); // also removes the old calls channel the first time 3.0.0 opens
    }
}
