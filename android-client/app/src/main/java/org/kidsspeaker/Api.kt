package org.kidsspeaker

import okhttp3.Credentials
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Сервер колонки (шлюз): тот же протокол, что у pi-client. */
open class Api(private val s: Settings) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(s.timeoutSec.toLong(), TimeUnit.SECONDS)
        .callTimeout(s.timeoutSec.toLong() + 10, TimeUnit.SECONDS)
        .build()

    /** Заголовки для всех запросов, в том числе стримов плеера и озвучки. */
    val headers: Map<String, String> = buildMap {
        if (s.user.isNotBlank()) put("Authorization", Credentials.basic(s.user, s.password, Charsets.UTF_8))
        if (s.token.isNotBlank()) put("X-Token", s.token)
    }

    private fun Request.Builder.auth(): Request.Builder = apply { headers.forEach { (k, v) -> header(k, v) } }

    private fun call(req: Request): JSONObject = http.newCall(req).execute().use { r ->
        if (!r.isSuccessful) throw IOException("HTTP ${r.code} ${req.url.encodedPath}")
        JSONObject(r.body!!.string())
    }

    /** Фраза → ответ шлюза. item — (модуль, id) того, что загружено в плеере. */
    open fun ask(wav: ByteArray, item: Pair<String, String>?, position: Double?): JSONObject {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("audio", "q.wav", wav.toRequestBody("audio/wav".toMediaType()))
            .addFormDataPart("device_id", s.deviceId)
            .addFormDataPart("source", item?.first ?: "")
            .addFormDataPart("now_playing", item?.second ?: "")
            .addFormDataPart("position", position?.let { "%.1f".format(java.util.Locale.ROOT, it) } ?: "")
            .build()
        return call(Request.Builder().url("${s.baseUrl}/ask").post(body).auth().build())
    }

    open fun progress(item: Pair<String, String>, position: Double, finished: Boolean) {
        val json = JSONObject().put("device_id", s.deviceId).put("source", item.first).put("item_id", item.second)
            .put("position", Math.round(position * 10) / 10.0).put("finished", finished)
        call(Request.Builder().url("${s.baseUrl}/progress")
            .post(json.toString().toRequestBody("application/json".toMediaType())).auth().build())
    }

    /** Лимиты для того, кто включил то, что играет на этом устройстве. */
    open fun policy(): JSONObject =
        call(Request.Builder().url("${s.baseUrl}/policy?device_id=${s.deviceId}").get().auth().build())

    /** Скачать озвученный ответ (wav). */
    open fun download(url: String): ByteArray = http.newCall(Request.Builder().url(url).get().auth().build())
        .execute().use { r ->
            if (!r.isSuccessful) throw IOException("HTTP ${r.code} $url")
            r.body!!.bytes()
        }
}
