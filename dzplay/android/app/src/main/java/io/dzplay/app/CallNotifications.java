package io.dzplay.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;

/**
 * The incoming-call notification: full-screen over the lock screen (where Android allows it),
 * ringtone + vibration until answered, declined or CALL_RING_TIMEOUT. It never shows who calls:
 * only "مكالمة واردة على DZPLAY" (the app shows the rest after it opens).
 */
final class CallNotifications {
    static final String CALL_CHANNEL = "dz_calls";
    static final String MESSAGE_CHANNEL = "dz_messages";
    static final int CALL_ID = 4100;
    static final int MESSAGE_ID = 4200;

    private CallNotifications() {
    }

    private static void channels(Context c) {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel calls = new NotificationChannel(CALL_CHANNEL, c.getString(R.string.callChannel), NotificationManager.IMPORTANCE_HIGH);
        calls.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE), new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        calls.enableVibration(true);
        calls.setVibrationPattern(new long[]{0, 800, 600, 800, 600});
        calls.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(calls);
        nm.createNotificationChannel(new NotificationChannel(MESSAGE_CHANNEL, c.getString(R.string.messageChannel), NotificationManager.IMPORTANCE_HIGH));
    }

    private static PendingIntent open(Context c, String callId, boolean answer, int code) {
        Intent i = new Intent(c, MainActivity.class)
                .setData(Uri.parse(MainActivity.HOME))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (callId != null) {
            i.putExtra(MainActivity.EXTRA_CALL, callId).putExtra(MainActivity.EXTRA_ANSWER, answer);
        }
        return PendingIntent.getActivity(c, code, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    static void showIncoming(Context c, String callId, int ringSeconds) {
        channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        PendingIntent decline = PendingIntent.getBroadcast(c, 2, new Intent(c, DeclineReceiver.class)
                .putExtra(MainActivity.EXTRA_CALL, callId), PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, CALL_CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(c.getString(R.string.appName))
                .setContentText(c.getString(R.string.incomingCall))
                .setCategory(Notification.CATEGORY_CALL)
                .setPriority(Notification.PRIORITY_MAX)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setOngoing(true)
                .setAutoCancel(true)
                .setContentIntent(open(c, callId, false, 1))
                .addAction(new Notification.Action.Builder(null, c.getString(R.string.decline), decline).build())
                .addAction(new Notification.Action.Builder(null, c.getString(R.string.answer), open(c, callId, true, 3)).build());
        boolean fullScreen = Build.VERSION.SDK_INT < 34 || nm.canUseFullScreenIntent();
        if (fullScreen) {
            b.setFullScreenIntent(open(c, callId, false, 4), true);
        }
        if (Build.VERSION.SDK_INT >= 26) {
            b.setTimeoutAfter(Math.max(10, ringSeconds) * 1000L); // never a stale ring
        }
        Notification n = b.build();
        n.flags |= Notification.FLAG_INSISTENT; // ring until answered / declined / timeout
        nm.notify(CALL_ID, n);
    }

    static void showMessage(Context c) {
        channels(c);
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(c, MESSAGE_CHANNEL) : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(c.getString(R.string.appName))
                .setContentText(c.getString(R.string.newMessage))
                .setAutoCancel(true)
                .setContentIntent(open(c, null, false, 5));
        nm.notify(MESSAGE_ID, b.build());
    }

    static void cancelIncoming(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.cancel(CALL_ID);
        }
    }
}
