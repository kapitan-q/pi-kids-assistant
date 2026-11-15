package org.kidsspeaker

import android.util.Log
import org.json.JSONObject

/** Что сейчас делает колонка — для экрана. */
enum class Status { IDLE, LISTENING, THINKING, SPEAKING, ERROR }

/**
 * Разговор с колонкой — логика Client из pi-client: слово активации (или кнопка) → пауза сказки →
 * запись фразы → шлюз → ответ голосом → команда плееру → (если это разговор) ещё реплики без слова
 * активации. Без Android-зависимостей, кроме логов, — тестируется с подделками.
 */
class Dialog(
    private val s: Settings,
    private val api: Api,
    private val mic: FrameSource,
    private val recorder: UtteranceRecorder,
    private val player: TalePlayer,
    private val sounds: Sounds,
    private val volume: VolumeControl,
    private val onStatus: (Status, String?) -> Unit = { _, _ -> },
    private val sleep: (Long) -> Unit = Thread::sleep,
) {
    private fun log(msg: String) = runCatching { Log.i("Dialog", msg) }

    fun applyLimits(lim: JSONObject?) {
        if (lim == null) return
        volume.limit(lim.optInt("max_volume", 100))
        if (lim.has("allow_playback") && !lim.optBoolean("allow_playback", true) && player.loaded()) {
            log("режим ${lim.optString("mode")}: сказки нельзя — стоп")
            player.stop()
        }
        // сказка началась до ночного режима — таймер задним числом
        if (!lim.isNull("stop_after_sec") && lim.has("stop_after_sec") && !player.timerActive && player.playing()) {
            player.setTimer(lim.getInt("stop_after_sec"))
        }
    }

    fun applyAction(act: JSONObject?, lim: JSONObject?, wasPlaying: Boolean) {
        if (act == null) return
        log("действие: $act")
        when (act.optString("type")) {
            "play" -> {
                val item = if (act.has("item_id")) act.getString("source") to act.getString("item_id") else null
                val stopAfter = lim?.takeIf { it.has("stop_after_sec") && !it.isNull("stop_after_sec") }
                    ?.getInt("stop_after_sec")
                val start = if (act.has("start") && !act.isNull("start")) act.getDouble("start") else null
                player.play(act.getString("url"), stopAfter, item, start)
            }
            "stop" -> player.stop()
            "resume" -> player.resume()
            "seek" -> {
                player.seek(act.optInt("seconds", 30), act.optString("mode") == "absolute")
                player.resume()
            }
            "volume" -> {
                if (act.has("level") && !act.isNull("level")) volume.set(act.getInt("level"))
                else volume.change(act.optInt("change", 0))
                if (wasPlaying) player.resume()
            }
        }
    }

    /** Отправить фразу, проиграть ответ, выполнить команду. null — сервер не ответил по делу. */
    private fun exchange(samples: ShortArray, wasPlaying: Boolean): JSONObject? {
        sounds.tone("done")
        onStatus(Status.THINKING, null)
        val item = if (player.loaded()) player.item else null
        val resp = try {
            api.ask(Wav.encode(samples), item, if (item != null) player.position() else null)
        } catch (e: Exception) {
            log("сервер недоступен: $e")
            onStatus(Status.ERROR, "Нет связи с сервером")
            sounds.tone("error")
            return null
        }
        log("услышал: ${resp.optString("heard")} -> ${resp.optString("reply")}")
        if (resp.optString("heard").isEmpty()) return null       // ложное срабатывание
        applyLimits(resp.optJSONObject("limits"))
        val speech = resp.optString("speech_url").takeIf { it.isNotEmpty() && it != "null" }
        if (speech != null) {
            onStatus(Status.SPEAKING, resp.optString("reply"))
            try { sounds.playWav(api.download(speech)) } catch (e: Exception) { log("ответ не проигран: $e") }
        }
        applyAction(resp.optJSONObject("action"), resp.optJSONObject("limits"), wasPlaying)
        return resp
    }

    /** Слово активации или кнопка: пауза сказки, сигнал, запись, ответ. */
    fun onWake(): JSONObject? {
        val wasPlaying = player.playing()
        if (wasPlaying) player.pause()
        var resp: JSONObject? = null
        try {
            sounds.tone("wake")
            mic.drain()                     // не отправлять свой же сигнал
            onStatus(Status.LISTENING, null)
            val samples = recorder.record() ?: return null.also { log("тишина после слова активации") }
            resp = exchange(samples, wasPlaying)
            return resp
        } finally {
            // любая неудача — сказка не должна зависнуть на паузе
            if (wasPlaying && resp == null) player.resume()
        }
    }

    /** Слушаем дальше без слова активации, только если это был разговор. */
    fun wantsFollowUp(resp: JSONObject?): Boolean = resp != null &&
        resp.optString("reply").isNotEmpty() && resp.isNull("action") &&
        !resp.optBoolean("blocked") && !resp.optBoolean("parent_command") && !player.playing()

    private fun followUp(waitSec: Float): JSONObject? {
        sleep(s.echoGuardMs.toLong())      // хвост ответа ещё звучит в Bluetooth
        mic.drain()
        if (s.followUpTone) { sounds.tone("listen"); mic.drain() }
        onStatus(Status.LISTENING, null)
        val samples = recorder.record(waitSec) ?: return null.also { log("продолжения нет") }
        return exchange(samples, wasPlaying = false)
    }

    /** Полный разговор. Сервер может сам попросить ответ (expect_reply: знакомство) — такие реплики
     *  ждём дольше и не считаем в maxFollowUps. */
    fun run() {
        try {
            var resp = onWake()
            var followUps = 0
            repeat(30) {                   // предохранитель от бесконечного диалога
                if (resp?.optBoolean("expect_reply") == true) {
                    resp = followUp(s.expectReplySec)
                    return@repeat
                }
                if (s.followUpSec <= 0 || followUps >= s.maxFollowUps || !wantsFollowUp(resp)) return
                followUps++
                resp = followUp(s.followUpSec)
            }
        } finally {
            onStatus(Status.IDLE, null)
        }
    }
}
