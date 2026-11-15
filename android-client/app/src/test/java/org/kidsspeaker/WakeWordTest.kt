package org.kidsspeaker

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Порт openWakeWord совпадает с Python-эталоном (tools/oww_reference.py) на ровно том же звуке
 * (padded_*.wav): мел-признаки и эмбеддинг первого куска и score каждого куска.
 * Эталон пересобирается при смене моделей.
 */
class WakeWordTest {
    private val assets = File("src/main/assets")
    private val ref = JSONObject(javaClass.getResource("/reference.json")!!.readText())

    private fun model() = WakeWord(File(assets, "melspectrogram.onnx").readBytes(),
        File(assets, "embedding_model.onnx").readBytes(), File(assets, "hey_jarvis_v0.1.onnx").readBytes())

    private fun clip(name: String): ShortArray {
        val (pcm, rate, ch) = Wav.decode(javaClass.getResource("/padded_$name")!!.readBytes())
        assertEquals(16000, rate); assertEquals(1, ch)
        return pcm
    }

    private fun JSONArray.floats() = (0 until length()).map { getDouble(it) }

    @Test
    fun scoresMatchPythonReference() {
        model().use { w ->
            for (name in listOf("hey_jarvis_Zira.wav", "hey_jarvis_David.wav", "other_ru.wav")) {
                w.reset()
                val x = clip(name)
                val ours = (x.indices step WakeWord.CHUNK).map { w.process(x.copyOfRange(it, it + WakeWord.CHUNK)) }
                val r = ref.getJSONObject(name)
                val theirs = r.getJSONArray("scores").floats()
                assertEquals(theirs.size, ours.size)
                ours.zip(theirs).forEachIndexed { i, (a, b) -> assertEquals("$name кусок $i", b, a.toDouble(), 1e-3) }
                r.getJSONArray("mel0").floats().zip(w.firstMel!!.toList())
                    .forEach { (b, a) -> assertEquals("$name mel", b, a.toDouble(), 1e-3) }
                r.getJSONArray("emb0").floats().zip(w.firstEmb!!.toList())
                    .forEach { (b, a) -> assertEquals("$name emb", b, a.toDouble(), 1e-3) }
            }
        }
    }

    @Test
    fun heyJarvisIsDetectedAndRussianPhraseIsNot() {
        model().use { w ->
            fun max(name: String): Float {
                w.reset()
                val x = clip(name)
                return (x.indices step WakeWord.CHUNK).maxOf { w.process(x.copyOfRange(it, it + WakeWord.CHUNK)) }
            }
            assertTrue(max("hey_jarvis_Zira.wav") > 0.9f)
            assertTrue(max("hey_jarvis_David.wav") > 0.9f)
            assertTrue(max("other_ru.wav") < 0.1f)
        }
    }
}
