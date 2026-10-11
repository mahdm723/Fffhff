package io.dzplay.app;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.cert.Certificate;
import java.security.spec.ECGenParameterSpec;
import java.util.ArrayList;
import java.util.List;

/**
 * V6 phase 14 (3.2.0): the app proves to the server that it is OUR app on a real phone (Android Key Attestation).
 *
 * First launch: a key is created inside the phone's secure hardware, bound to a one-time challenge from the server;
 * the hardware signs a certificate chain for it (up to Google's root) that names our package and signing
 * certificate. The server checks it and answers with an access token, set here as the dz_app cookie of the WebView
 * (Android browsers never get it, so they only see «حمّل التطبيق»).
 * Later launches (and every few hours): the same key signs a fresh challenge.
 *
 * Nothing personal is sent: a challenge, a certificate chain (no phone number, no IMEI, no account), the version.
 */
final class AppAttest {
    interface Done {
        void run(boolean ok);
    }

    private static final String ALIAS = "dz_app_key";
    private static final String KEY_ID = "attest_key_id";
    private static final String AT = "attest_at";
    private static final long RENEW_MS = 4L * 3600 * 1000; // the token lasts APP_TOKEN_HOURS (12 h) on the server
    private static final List<Done> waiting = new ArrayList<>();
    private static boolean running = false;

    private AppAttest() {
    }

    /** Is the access cookie old enough to renew (or missing)? */
    static boolean due(Context c) {
        long at = prefs(c).getLong(AT, 0);
        String cookies = CookieManager.getInstance().getCookie(MainActivity.HOME);
        return System.currentTimeMillis() - at > RENEW_MS || cookies == null || !cookies.contains("dz_app=");
    }

    /** Gets a fresh access cookie in the background, then calls done on the UI thread (ok = the server accepted).
     *  Called on the UI thread; a call while one is running waits for that one. */
    static void refresh(Activity a, Done done) {
        waiting.add(done);
        if (running) {
            return;
        }
        running = true;
        Context c = a.getApplicationContext();
        new Thread(() -> {
            boolean ok = false;
            try {
                ok = prove(c) || attest(c);
            } catch (Exception ignored) {
                // no network or no secure hardware: the server shows «تعذّر التحقق» with a retry button
            }
            boolean result = ok;
            a.runOnUiThread(() -> {
                running = false;
                List<Done> all = new ArrayList<>(waiting);
                waiting.clear();
                for (Done d : all) {
                    d.run(result);
                }
            });
        }, "dz-attest").start();
    }

    // ------------------------------------------------------------------------------------------------ steps

    /** A later launch: sign a challenge with the key the server already knows. */
    private static boolean prove(Context c) throws Exception {
        String keyId = prefs(c).getString(KEY_ID, null);
        KeyStore ks = keyStore();
        if (keyId == null || !ks.containsAlias(ALIAS)) {
            return false;
        }
        JSONObject ch = challenge();
        Signature s = Signature.getInstance("SHA256withECDSA");
        s.initSign((PrivateKey) ks.getKey(ALIAS, null));
        s.update(decode(ch.getString("challenge")));
        JSONObject body = new JSONObject()
                .put("challenge_id", ch.getString("id"))
                .put("key_id", keyId)
                .put("signature", Base64.encodeToString(s.sign(), Base64.NO_WRAP))
                .put("version_code", BuildConfig.VERSION_CODE);
        Answer r = post("/api/app/proof", body);
        if (r.status == 200) {
            save(c, r.json, keyId);
            return true;
        }
        if (r.status == 404 || r.status == 403) { // the server does not know (or no longer trusts) this key: start over
            ks.deleteEntry(ALIAS);
            prefs(c).edit().remove(KEY_ID).apply();
        }
        return false;
    }

    /** First launch (or a new key): Key Attestation. */
    private static boolean attest(Context c) throws Exception {
        JSONObject ch = challenge();
        KeyStore ks = keyStore();
        if (ks.containsAlias(ALIAS)) {
            ks.deleteEntry(ALIAS);
        }
        KeyPairGenerator g = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore");
        g.initialize(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setAttestationChallenge(decode(ch.getString("challenge")))
                .build());
        g.generateKeyPair();
        Certificate[] chain = keyStore().getCertificateChain(ALIAS);
        if (chain == null || chain.length < 2) {
            return false; // no hardware attestation on this phone
        }
        JSONArray certs = new JSONArray();
        for (Certificate cert : chain) {
            certs.put(Base64.encodeToString(cert.getEncoded(), Base64.NO_WRAP));
        }
        JSONObject body = new JSONObject()
                .put("challenge_id", ch.getString("id"))
                .put("chain", certs)
                .put("version_code", BuildConfig.VERSION_CODE);
        Answer r = post("/api/app/attest", body);
        if (r.status != 200) {
            return false;
        }
        String keyId = hex(MessageDigest.getInstance("SHA-256").digest(chain[0].getPublicKey().getEncoded()));
        save(c, r.json, keyId);
        return true;
    }

    private static void save(Context c, JSONObject answer, String keyId) throws Exception {
        String token = answer.getString("token");
        int maxAge = answer.optInt("max_age", 3600);
        CookieManager cm = CookieManager.getInstance();
        cm.setCookie(MainActivity.HOME, "dz_app=" + token + "; Max-Age=" + maxAge + "; Path=/; Secure; HttpOnly; SameSite=Lax");
        cm.flush();
        prefs(c).edit().putString(KEY_ID, keyId).putLong(AT, System.currentTimeMillis()).apply();
    }

    // ------------------------------------------------------------------------------------------------ HTTP

    private static final class Answer {
        final int status;
        final JSONObject json;

        Answer(int status, JSONObject json) {
            this.status = status;
            this.json = json;
        }
    }

    private static JSONObject challenge() throws Exception {
        Answer r = request("GET", "/api/app/challenge", null);
        if (r.status != 200) {
            throw new IOException("challenge " + r.status);
        }
        return r.json;
    }

    private static Answer post(String path, JSONObject body) throws Exception {
        return request("POST", path, body);
    }

    private static Answer request(String method, String path, JSONObject body) throws Exception {
        HttpURLConnection h = (HttpURLConnection) new URL("https://" + MainActivity.HOST + path).openConnection();
        h.setConnectTimeout(15000);
        h.setReadTimeout(20000);
        h.setRequestMethod(method);
        h.setRequestProperty("Accept", "application/json");
        h.setRequestProperty("X-DZ-Requested", "1");
        h.setUseCaches(false);
        if (body != null) {
            byte[] out = body.toString().getBytes(StandardCharsets.UTF_8);
            h.setDoOutput(true);
            h.setRequestProperty("Content-Type", "application/json");
            h.setFixedLengthStreamingMode(out.length);
            try (OutputStream o = h.getOutputStream()) {
                o.write(out);
            }
        }
        int status = h.getResponseCode();
        InputStream in = status < 400 ? h.getInputStream() : h.getErrorStream();
        String text = "";
        if (in != null) {
            try (InputStream i = in; ByteArrayOutputStream buf = new ByteArrayOutputStream()) {
                byte[] b = new byte[4096];
                int n;
                while ((n = i.read(b)) > 0 && buf.size() < 64 * 1024) {
                    buf.write(b, 0, n);
                }
                text = buf.toString("UTF-8");
            }
        }
        h.disconnect();
        JSONObject json;
        try {
            json = new JSONObject(text);
        } catch (Exception e) {
            json = new JSONObject();
        }
        return new Answer(status, json);
    }

    // ------------------------------------------------------------------------------------------------ helpers

    private static KeyStore keyStore() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        return ks;
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(MainActivity.PREFS, Context.MODE_PRIVATE);
    }

    private static byte[] decode(String urlSafe) {
        return Base64.decode(urlSafe, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
    }

    private static String hex(byte[] bytes) {
        StringBuilder sb = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }
}
