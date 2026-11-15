package org.kidsspeaker

import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/** Настройки колонки (как config.toml у pi-client). Сохранить — служба перезапускается. */
class SettingsActivity : AppCompatActivity() {
    private val fields = linkedMapOf<String, EditText>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val s = Settings.load(this)
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(40, 40, 40, 40) }

        fun field(key: String, label: String, value: String, type: Int = InputType.TYPE_CLASS_TEXT) {
            col.addView(TextView(this).apply { text = label; setPadding(0, 24, 0, 4) })
            val e = EditText(this).apply { setText(value); inputType = type; isSingleLine = true }
            fields[key] = e
            col.addView(e)
        }
        val num = InputType.TYPE_CLASS_NUMBER
        val dec = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
        field("server", "Адрес сервера", s.server, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        field("user", "Пользователь колонки (basic-auth)", s.user)
        field("password", "Пароль колонки", s.password,
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        field("token", "GATEWAY_TOKEN (если задан)", s.token)
        field("device_id", "Имя устройства (device_id)", s.deviceId)
        field("wake_threshold", "Порог слова активации (0.3–0.8)", s.wakeThreshold.toString(), dec)
        field("follow_up_sec", "Сколько ждать продолжения разговора, с (0 — не ждать)", s.followUpSec.toString(), dec)
        field("max_follow_ups", "Сколько реплик подряд без слова активации", s.maxFollowUps.toString(), num)
        field("end_silence_ms", "Пауза, после которой фраза закончена, мс", s.endSilenceMs.toString(), num)
        field("default_volume", "Громкость по умолчанию (10–100)", s.defaultVolume.toString(), num)

        col.addView(Button(this).apply {
            text = "Сохранить"
            setOnClickListener { save(s) }
        })
        setContentView(ScrollView(this).apply { addView(col) })
    }

    private fun v(key: String) = fields.getValue(key).text.toString().trim()

    private fun save(old: Settings) {
        val new = try {
            old.copy(
                server = v("server"), user = v("user"), password = fields.getValue("password").text.toString(),
                token = v("token"), deviceId = v("device_id").ifEmpty { "kids-room" },
                wakeThreshold = v("wake_threshold").toFloat().coerceIn(0.1f, 0.95f),
                followUpSec = v("follow_up_sec").toFloat().coerceIn(0f, 30f),
                maxFollowUps = v("max_follow_ups").toInt().coerceIn(0, 20),
                endSilenceMs = v("end_silence_ms").toInt().coerceIn(300, 3000),
                defaultVolume = v("default_volume").toInt().coerceIn(10, 100),
            )
        } catch (e: NumberFormatException) {
            Toast.makeText(this, "Проверьте числа", Toast.LENGTH_SHORT).show()
            return
        }
        Settings.save(this, new)
        SpeakerService.restart(this)
        Toast.makeText(this, "Сохранено", Toast.LENGTH_SHORT).show()
        finish()
    }
}
