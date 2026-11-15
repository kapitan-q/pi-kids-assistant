package org.kidsspeaker

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import java.nio.FloatBuffer

/**
 * Слово активации — потоковый конвейер openWakeWord (как в pi-client), на ONNX Runtime:
 * звук 16 кГц кусками по 1280 отсчётов (80 мс) → мел-спектрограмма (последние 1280+480 отсчётов,
 * x/10+2) → эмбеддинг по последним 76 мел-кадрам → модель слова по последним 16 эмбеддингам → 0..1.
 * Сверяется с Python-эталоном (android-client/tools/oww_reference.py) в WakeWordTest.
 */
class WakeWord(mel: ByteArray, embedding: ByteArray, word: ByteArray) : AutoCloseable {
    companion object {
        const val CHUNK = 1280
        private const val CONTEXT = 480
        private const val MEL_BINS = 32
        private const val MEL_WINDOW = 76
        private const val EMB = 96
        private const val FEATURES = 16
    }

    private val env = OrtEnvironment.getEnvironment()
    private val opts = OrtSession.SessionOptions().apply { setIntraOpNumThreads(1); setInterOpNumThreads(1) }
    private val melSession = env.createSession(mel, opts)
    private val embSession = env.createSession(embedding, opts)
    private val wordSession = env.createSession(word, opts)

    private var raw = ShortArray(0)
    private val melBuf = ArrayDeque<FloatArray>()
    private val feat = ArrayDeque<FloatArray>()
    private var calls = 0

    /** Первые мел-кадры и эмбеддинг после reset() — для сверки с эталоном. */
    var firstMel: FloatArray? = null; private set
    var firstEmb: FloatArray? = null; private set

    init { reset() }

    fun reset() {
        raw = ShortArray(0)
        melBuf.clear()
        repeat(MEL_WINDOW) { melBuf.addLast(FloatArray(MEL_BINS) { 1f }) }
        feat.clear()
        repeat(FEATURES) { feat.addLast(FloatArray(EMB)) }
        calls = 0
        firstMel = null
        firstEmb = null
    }

    /** Первые 5 кадров после reset() score не учитывается (как в openWakeWord): буферы ещё не заполнены. */
    val warmedUp: Boolean get() = calls > 5

    /** chunk — ровно 1280 отсчётов. Возвращает score 0..1. */
    fun process(chunk: ShortArray): Float {
        require(chunk.size == CHUNK) { "нужно $CHUNK отсчётов" }
        val joined = raw + chunk
        raw = joined.copyOfRange(maxOf(0, joined.size - CHUNK - CONTEXT), joined.size)

        // мел-спектрограмма
        val input = FloatArray(raw.size) { raw[it].toFloat() }
        val mel = run(melSession, input, longArrayOf(1, raw.size.toLong()))
        val frames = mel.size / MEL_BINS
        for (f in 0 until frames) {
            melBuf.addLast(FloatArray(MEL_BINS) { mel[f * MEL_BINS + it] / 10f + 2f })
        }
        while (melBuf.size > 970) melBuf.removeFirst()
        if (firstMel == null) firstMel = FloatArray(frames * MEL_BINS) { mel[it] / 10f + 2f }

        // эмбеддинг по последним 76 кадрам
        val win = FloatArray(MEL_WINDOW * MEL_BINS)
        melBuf.toList().takeLast(MEL_WINDOW).forEachIndexed { i, row -> row.copyInto(win, i * MEL_BINS) }
        val emb = run(embSession, win, longArrayOf(1, MEL_WINDOW.toLong(), MEL_BINS.toLong(), 1))
        if (firstEmb == null) firstEmb = emb.copyOf()
        feat.addLast(emb)
        while (feat.size > 120) feat.removeFirst()

        // модель слова
        val x = FloatArray(FEATURES * EMB)
        feat.toList().takeLast(FEATURES).forEachIndexed { i, row -> row.copyInto(x, i * EMB) }
        val score = run(wordSession, x, longArrayOf(1, FEATURES.toLong(), EMB.toLong()))[0]
        calls++
        return score
    }

    private fun run(s: OrtSession, data: FloatArray, shape: LongArray): FloatArray {
        OnnxTensor.createTensor(env, FloatBuffer.wrap(data), shape).use { t ->
            s.run(mapOf(s.inputNames.first() to t)).use { r ->
                val v = r[0] as OnnxTensor
                val fb = v.floatBuffer
                val out = FloatArray(fb.remaining())
                fb.get(out)
                return out
            }
        }
    }

    override fun close() {
        melSession.close(); embSession.close(); wordSession.close(); opts.close()
    }
}
