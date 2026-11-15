package org.kidsspeaker

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings as SysSettings
import android.view.Gravity
import android.view.MotionEvent
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/** Экран колонки: статус, большая кнопка «Говорить», «Стоп». Настройки — долгим (3 с) нажатием на заголовок. */
class MainActivity : AppCompatActivity() {
    private lateinit var statusView: TextView
    private lateinit var detailView: TextView

    @SuppressLint("ClickableViewAccessibility", "SetTextI18n")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(48, 96, 48, 48)
            setBackgroundColor(Color.parseColor("#F6F5F2"))
        }
        val title = TextView(this).apply {
            text = "Колонка"; textSize = 28f; setTypeface(typeface, Typeface.BOLD)
            setTextColor(Color.parseColor("#1F1D1A")); gravity = Gravity.CENTER
        }
        // три секунды держать заголовок — настройки (чтобы дети не попадали случайно)
        var downAt = 0L
        title.setOnTouchListener { _, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> downAt = e.eventTime
                MotionEvent.ACTION_UP -> if (e.eventTime - downAt >= 3000) {
                    startActivity(Intent(this, SettingsActivity::class.java))
                }
            }
            true
        }
        statusView = TextView(this).apply {
            textSize = 24f; gravity = Gravity.CENTER; setPadding(0, 64, 0, 16)
            setTextColor(Color.parseColor("#2F6F5E"))
        }
        detailView = TextView(this).apply {
            textSize = 18f; gravity = Gravity.CENTER; setTextColor(Color.parseColor("#6B665E")); minLines = 3
        }
        val talk = Button(this).apply {
            text = getString(R.string.talk); textSize = 32f; setTextColor(Color.WHITE)
            setBackgroundColor(Color.parseColor("#2F6F5E"))
            setOnClickListener { SpeakerService.start(this@MainActivity, SpeakerService.ACTION_TALK) }
        }
        val stop = Button(this).apply {
            text = getString(R.string.stop); textSize = 22f
            setOnClickListener { SpeakerService.start(this@MainActivity, SpeakerService.ACTION_STOP_TALE) }
        }
        root.addView(title)
        root.addView(statusView)
        root.addView(detailView)
        root.addView(talk, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 480).apply { topMargin = 48 })
        root.addView(stop, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = 32 })
        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        SpeakerService.listener = { st, text -> runOnUiThread { show(st, text) } }
        show(SpeakerService.status.first, SpeakerService.status.second)
        ensurePermissionsAndStart()
    }

    private fun show(st: Status, text: String?) {
        statusView.text = when (st) {
            Status.IDLE -> "Скажи «Hey Jarvis»"
            Status.LISTENING -> "Слушаю…"
            Status.THINKING -> "Думаю…"
            Status.SPEAKING -> "Отвечаю"
            Status.ERROR -> "Что-то не так"
        }
        detailView.text = text ?: ""
    }

    private var asked = false

    private fun ensurePermissionsAndStart() {
        val wanted = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        val need = wanted.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (need.isNotEmpty() && !asked) {
            asked = true                      // спрашиваем один раз за показ экрана
            requestPermissions(need.toTypedArray(), 1)
            return
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            show(Status.ERROR, "Без микрофона колонка не услышит. Разрешите доступ в настройках телефона.")
            return
        }
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            runCatching {
                startActivity(Intent(SysSettings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName")))
            }
        }
        if (!SpeakerService.running) SpeakerService.start(this)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        ensurePermissionsAndStart()
    }

    override fun onPause() {
        SpeakerService.listener = null
        asked = false
        super.onPause()
    }
}
