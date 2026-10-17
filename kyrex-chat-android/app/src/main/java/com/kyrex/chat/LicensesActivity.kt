package com.kyrex.chat

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.*

class LicensesActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 64, 32, 32) }
        layout.addView(Button(this).apply { text = "Corresponding source code"; setOnClickListener {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(BuildConfig.SOURCE_URL)))
        } })
        layout.addView(TextView(this).apply { text = listOf("NOTICE.txt", "COPYING-AGPL.txt", "LICENSE-MIT.txt")
            .joinToString("\n\n") { name -> assets.open(name).bufferedReader().use { it.readText() } } })
        setContentView(ScrollView(this).apply { addView(layout) })
    }
}
