package org.kidsspeaker

import android.content.Context

/** Настройки колонки — те же, что в pi-client/config.toml. Хранятся в SharedPreferences. */
data class Settings(
    val server: String = BuildConfig.DEFAULT_SERVER,
    val user: String = "speaker",
    val password: String = "",
    val token: String = "",
    val deviceId: String = "kids-room",
    val timeoutSec: Int = 45,
    val wakeThreshold: Float = 0.5f,
    val endSilenceMs: Int = 900,
    val noSpeechSec: Float = 4f,
    val maxRecordSec: Float = 10f,
    val followUpSec: Float = 6f,
    val maxFollowUps: Int = 5,
    val expectReplySec: Float = 10f,
    val echoGuardMs: Int = 400,
    val followUpTone: Boolean = true,
    val defaultVolume: Int = 50,
    val fadeSec: Float = 20f,
) {
    val baseUrl: String get() = server.trimEnd('/')
    val configured: Boolean get() = server.isNotBlank() && (user.isBlank() || password.isNotBlank())

    companion object {
        private const val PREFS = "settings"

        fun load(ctx: Context): Settings {
            val p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val d = Settings()
            return Settings(
                server = p.getString("server", d.server)!!,
                user = p.getString("user", d.user)!!,
                password = p.getString("password", d.password)!!,
                token = p.getString("token", d.token)!!,
                deviceId = p.getString("device_id", d.deviceId)!!,
                timeoutSec = p.getInt("timeout_sec", d.timeoutSec),
                wakeThreshold = p.getFloat("wake_threshold", d.wakeThreshold),
                endSilenceMs = p.getInt("end_silence_ms", d.endSilenceMs),
                noSpeechSec = p.getFloat("no_speech_sec", d.noSpeechSec),
                maxRecordSec = p.getFloat("max_record_sec", d.maxRecordSec),
                followUpSec = p.getFloat("follow_up_sec", d.followUpSec),
                maxFollowUps = p.getInt("max_follow_ups", d.maxFollowUps),
                expectReplySec = p.getFloat("expect_reply_sec", d.expectReplySec),
                echoGuardMs = p.getInt("echo_guard_ms", d.echoGuardMs),
                followUpTone = p.getBoolean("follow_up_tone", d.followUpTone),
                defaultVolume = p.getInt("default_volume", d.defaultVolume),
                fadeSec = p.getFloat("fade_sec", d.fadeSec),
            )
        }

        fun save(ctx: Context, s: Settings) {
            ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString("server", s.server.trim())
                .putString("user", s.user.trim())
                .putString("password", s.password)
                .putString("token", s.token.trim())
                .putString("device_id", s.deviceId.trim())
                .putInt("timeout_sec", s.timeoutSec)
                .putFloat("wake_threshold", s.wakeThreshold)
                .putInt("end_silence_ms", s.endSilenceMs)
                .putFloat("no_speech_sec", s.noSpeechSec)
                .putFloat("max_record_sec", s.maxRecordSec)
                .putFloat("follow_up_sec", s.followUpSec)
                .putInt("max_follow_ups", s.maxFollowUps)
                .putFloat("expect_reply_sec", s.expectReplySec)
                .putInt("echo_guard_ms", s.echoGuardMs)
                .putBoolean("follow_up_tone", s.followUpTone)
                .putInt("default_volume", s.defaultVolume)
                .putFloat("fade_sec", s.fadeSec)
                .apply()
        }
    }
}
