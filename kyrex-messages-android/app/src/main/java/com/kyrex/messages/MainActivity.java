package com.kyrex.messages;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.provider.Settings;
import android.provider.Telephony;
import android.text.InputType;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.text.DateFormat;
import java.util.Date;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private CancellationSignal cancellation;
    private TextView permission;
    private TextView result;
    private EditText phrase;
    private EditText recipient;
    private Button scan;
    private boolean destroyed;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (20 * getResources().getDisplayMetrics().density);
        content.setPadding(padding, padding, padding, padding);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(content);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        setContentView(scroll);
        text(content, "Kyrex Messages Test", 26);
        text(content, "Check what your Samsung lets Kyrex read. This test stays on your phone: no uploads, no background reading, and no automatic sending. It does not connect to Kyrex Chat yet.", 17);
        text(content, "1. Allow message reading", 20);
        permission = text(content, "", 16);
        button(content, "Allow access", () -> new AlertDialog.Builder(this)
                .setTitle("Read messages on this phone?")
                .setMessage("The test reads up to 5,000 message records from the last 7 days when you tap Check history. Only counts and dates are displayed. Message text is used for your exact-text check and is not saved or uploaded.")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Continue", (dialog, which) -> requestPermissions(new String[]{Manifest.permission.READ_SMS}, 1)).show());
        button(content, "Open app permissions", () -> open(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.fromParts("package", getPackageName(), null))));
        text(content, "2. Check history and a known RCS message", 20);
        text(content, "In Google Messages, find a message from the last 7 days and confirm its details say RCS. Enter a distinctive part of its text exactly. This checks that specific message, not all RCS support.", 16);
        phrase = input(content, "Exact text from a known RCS message (optional)", InputType.TYPE_CLASS_TEXT);
        scan = button(content, "Check history", this::scanHistory);
        result = text(content, "History has not been checked.", 17);
        text(content, "3. Check reply handoff", 20);
        text(content, "Enter a trusted contact's number. This opens a test draft in your messaging app. You review it and tap Send there. Confirm SMS or RCS and the sending number in that app; Kyrex cannot verify delivery from this test.", 16);
        recipient = input(content, "Recipient phone number", InputType.TYPE_CLASS_PHONE);
        button(content, "Open test draft", this::openDraft);
        text(content, "Keep Google Messages as your default texting app. If Android blocks message permission, report that result; this test does not require terminal commands or replacing your messaging app.", 16);
    }

    @Override public void onResume() {
        super.onResume();
        permission.setText(checkSelfPermission(Manifest.permission.READ_SMS) == PackageManager.PERMISSION_GRANTED
                ? "Message reading permission granted. Tap Check history to test actual access."
                : "Message reading is not allowed yet. If Allow access is blocked, check Open app permissions.");
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == 1) {
            boolean granted = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
            permission.setText(granted ? "Permission granted. Now tap Check history."
                    : "Permission was denied or blocked by Android. Open app permissions to check. If SMS is unavailable there, tell us; this route is not proven on your phone.");
        }
    }

    private void scanHistory() {
        if (checkSelfPermission(Manifest.permission.READ_SMS) != PackageManager.PERMISSION_GRANTED) {
            result.setText("Allow message reading before checking history.");
            return;
        }
        String exact = phrase.getText().toString().trim();
        long end = System.currentTimeMillis();
        long start = end - 7L * 24 * 60 * 60 * 1000;
        scan.setEnabled(false);
        result.setText("Checking readable records on this phone…");
        CancellationSignal signal = new CancellationSignal();
        cancellation = signal;
        worker.execute(() -> {
            String report;
            try (Cursor cursor = getContentResolver().query(Telephony.Sms.CONTENT_URI,
                    new String[]{Telephony.Sms.DATE, Telephony.Sms.BODY},
                    "date >= ? AND date <= ?", new String[]{Long.toString(start), Long.toString(end)},
                    "date DESC", signal)) {
                if (cursor == null) throw new IllegalStateException();
                ProbeSummary summary = new ProbeSummary();
                while (!signal.isCanceled() && cursor.moveToNext()) {
                    if (summary.count == ProbeSummary.LIMIT) { summary.truncated = true; break; }
                    summary.add(cursor.getLong(0), cursor.getString(1), exact);
                }
                report = "Readable records in the last 7 days: " + summary.count
                        + (summary.truncated ? " (limit reached; coverage is incomplete)." : ".")
                        + (summary.count == 0 ? "\nNo records returned. Permission alone does not confirm access."
                        : "\nNewest: " + date(summary.newest) + "\nOldest: " + date(summary.oldest))
                        + "\n\n" + summary.matchReport(exact)
                        + "\n\nThis reads the Android SMS provider. MMS, attachments, full RCS coverage and background replies are not verified.";
            } catch (SecurityException error) {
                report = "Android blocked message history access. Permission may be restricted on this phone. No records were uploaded. Tell us this result.";
            } catch (RuntimeException error) {
                report = "History check could not finish. Try again; if it repeats, report this result. No message text was saved or uploaded.";
            }
            String completed = report;
            runOnUiThread(() -> {
                if (!destroyed && !signal.isCanceled()) {
                    result.setText(completed);
                    scan.setEnabled(true);
                }
            });
        });
    }

    private void openDraft() {
        String number = recipient.getText().toString().trim();
        if (!ProbeSummary.validRecipient(number)) { recipient.setError("Enter a phone number"); return; }
        Intent intent = new Intent(Intent.ACTION_SENDTO, Uri.fromParts("smsto", number, null));
        intent.putExtra("sms_body", "Kyrex message connection test");
        open(intent);
    }

    private void open(Intent intent) {
        try { startActivity(intent); }
        catch (ActivityNotFoundException | SecurityException error) {
            new AlertDialog.Builder(this).setMessage("Android could not open that screen. Check your phone's settings or messaging app directly.").setPositiveButton("OK", null).show();
        }
    }

    private String date(long millis) { return DateFormat.getDateTimeInstance().format(new Date(millis)); }
    private TextView text(LinearLayout content, String value, int size) {
        TextView view = new TextView(this);
        view.setText(value); view.setTextSize(size); view.setPadding(0, 16, 0, 12);
        content.addView(view); return view;
    }
    private EditText input(LinearLayout content, String hint, int type) {
        EditText view = new EditText(this);
        view.setHint(hint); view.setInputType(type);
        view.setSaveEnabled(false);
        content.addView(view); return view;
    }
    private Button button(LinearLayout content, String label, Runnable action) {
        Button view = new Button(this); view.setText(label); view.setAllCaps(false);
        view.setOnClickListener(v -> action.run()); content.addView(view); return view;
    }
    @Override public void onDestroy() {
        destroyed = true;
        if (cancellation != null) cancellation.cancel();
        worker.shutdownNow();
        super.onDestroy();
    }
}
