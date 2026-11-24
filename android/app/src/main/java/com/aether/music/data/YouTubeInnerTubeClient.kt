package com.aether.music.data

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import org.schabi.newpipe.extractor.NewPipe
import org.schabi.newpipe.extractor.ServiceList
import org.schabi.newpipe.extractor.localization.ContentCountry
import org.schabi.newpipe.extractor.localization.Localization
import org.schabi.newpipe.extractor.stream.DeliveryMethod
import org.schabi.newpipe.extractor.stream.StreamInfo
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

data class ResolvedStream(
    val url: String,
    val userAgent: String,
    val expiresAtMs: Long,
)

class YouTubeInnerTubeClient(
    val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(25, TimeUnit.SECONDS)
        .followRedirects(true)
        .followSslRedirects(true)
        .build()
) {
    private val streamCache = ConcurrentHashMap<String, ResolvedStream>()
    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private val artistSuffixRegex = Regex("\\s*(-\\s*Topic|Official|VEVO|Music)$", RegexOption.IGNORE_CASE)
    private val expireRegex = Regex("[?&]expire=(\\d+)")
    private val visitorDataRegex = Regex("\"(Cg[a-zA-Z0-9_%-]{10,})\"")

    @Volatile
    private var cachedVisitorData: String? = null

    @Volatile
    var lastResolveError: String? = null
        private set

    init {
        ensureNewPipeInitialized(httpClient)
    }

    private data class PlayerClientProfile(
        val name: String,
        val version: String,
        val userAgent: String,
        val clientHeaderId: String,
        val endpoint: String = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
        val extraClientFields: Map<String, Any> = emptyMap(),
        val sendOrigin: Boolean = false,
    )

    private val playerProfiles = listOf(
        PlayerClientProfile(
            name = "ANDROID_VR",
            version = "1.61.48",
            userAgent = "com.google.android.apps.youtube.vr.oculus/1.61.48 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
            clientHeaderId = "28",
            extraClientFields = mapOf(
                "deviceMake" to "Oculus",
                "deviceModel" to "Quest 3",
                "androidSdkVersion" to 32,
                "osName" to "Android",
                "osVersion" to "12L",
            ),
        ),
        PlayerClientProfile(
            name = "IOS",
            version = "20.10.4",
            userAgent = "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
            clientHeaderId = "5",
            extraClientFields = mapOf(
                "deviceMake" to "Apple",
                "deviceModel" to "iPhone16,2",
                "osName" to "iPhone",
                "osVersion" to "18.3.2.22D82",
            ),
        ),
        PlayerClientProfile(
            name = "ANDROID",
            version = "20.10.38",
            userAgent = "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip",
            clientHeaderId = "3",
            extraClientFields = mapOf(
                "androidSdkVersion" to 34,
                "osName" to "Android",
                "osVersion" to "14",
            ),
        ),
        PlayerClientProfile(
            name = "TVHTML5",
            version = "7.20250312.16.00",
            userAgent = "Mozilla/5.0 (ChromiumStylePlatform) Cobalt/Version",
            clientHeaderId = "7",
        ),
    )

    fun invalidateStream(videoId: String) {
        streamCache.remove(videoId)
    }

    fun resolveAudioStream(videoId: String, forceRefresh: Boolean = false): ResolvedStream? {
        val now = System.currentTimeMillis()
        if (!forceRefresh) {
            val cached = streamCache[videoId]
            if (cached != null && cached.expiresAtMs > now + 120_000L) {
                lastResolveError = null
                return cached
            }
        }

        val errors = mutableListOf<String>()

        // 1. Primary: NewPipeExtractor v0.27.1 (handles JS signature + n-param deobfuscation & fresh clients)
        try {
            val extracted = resolveWithNewPipe(videoId)
            if (extracted != null) {
                streamCache[videoId] = extracted
                lastResolveError = null
                return extracted
            }
            errors += "NewPipe: нет аудиопотоков"
        } catch (e: Exception) {
            errors += "NewPipe: ${e.message ?: e.javaClass.simpleName}"
        }

        // 2. Fallback: Direct InnerTube API with live visitorData
        val visitorData = fetchVisitorData()
        for (profile in playerProfiles) {
            try {
                val resolved = requestPlayerStream(videoId, profile, visitorData)
                if (resolved != null) {
                    streamCache[videoId] = resolved
                    lastResolveError = null
                    return resolved
                }
            } catch (e: Exception) {
                errors += "${profile.name}: ${e.message ?: e.javaClass.simpleName}"
            }
        }

        lastResolveError = errors.firstOrNull() ?: "Не удалось получить аудиопоток с YouTube"
        return null
    }

    private fun resolveWithNewPipe(videoId: String): ResolvedStream? {
        ensureNewPipeInitialized(httpClient)
        val watchUrl = "https://www.youtube.com/watch?v=$videoId"
        val info = StreamInfo.getInfo(ServiceList.YouTube, watchUrl)

        val audioStreams = info.audioStreams.orEmpty()
            .filter { it.isUrl && !it.content.isNullOrBlank() }

        val bestAudio = audioStreams.maxByOrNull { stream ->
            var score = stream.averageBitrate.toDouble()
            if (stream.deliveryMethod == DeliveryMethod.PROGRESSIVE_HTTP) {
                score += 10_000.0
            }
            val codec = stream.codec.orEmpty().lowercase()
            if (codec.contains("mp4a")) score += 400.0
            else if (codec.contains("opus")) score += 350.0
            score
        }

        val url = bestAudio?.content?.takeIf { it.isNotBlank() }
            ?: info.hlsUrl?.takeIf { it.isNotBlank() }
            ?: return null

        return ResolvedStream(
            url = url,
            userAgent = OkHttpNewPipeDownloader.DEFAULT_USER_AGENT,
            expiresAtMs = parseExpiryMs(url),
        )
    }

    private fun fetchVisitorData(): String? {
        cachedVisitorData?.let { return it }
        return try {
            val req = Request.Builder()
                .url("https://www.youtube.com/sw.js_data")
                .header("User-Agent", OkHttpNewPipeDownloader.DEFAULT_USER_AGENT)
                .get()
                .build()
            httpClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return null
                val body = resp.body?.string() ?: return null
                val found = visitorDataRegex.find(body)?.groupValues?.getOrNull(1)
                if (!found.isNullOrBlank()) {
                    cachedVisitorData = found
                }
                found
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun requestPlayerStream(
        videoId: String,
        profile: PlayerClientProfile,
        visitorData: String?,
    ): ResolvedStream? {
        val clientJson = JSONObject().apply {
            put("clientName", profile.name)
            put("clientVersion", profile.version)
            put("hl", "ru")
            put("gl", "US")
            if (!visitorData.isNullOrBlank()) {
                put("visitorData", visitorData)
            }
            for ((k, v) in profile.extraClientFields) {
                put(k, v)
            }
        }
        val contextJson = JSONObject().apply {
            put("client", clientJson)
        }
        val payload = JSONObject().apply {
            put("videoId", videoId)
            put("context", contextJson)
            put("contentCheckOk", true)
            put("racyCheckOk", true)
        }

        val reqBuilder = Request.Builder()
            .url(profile.endpoint)
            .header("User-Agent", profile.userAgent)
            .header("X-YouTube-Client-Name", profile.clientHeaderId)
            .header("X-YouTube-Client-Version", profile.version)
            .post(payload.toString().toRequestBody(jsonMediaType))

        if (!visitorData.isNullOrBlank()) {
            reqBuilder.header("X-Goog-Visitor-Id", visitorData)
        }
        if (profile.sendOrigin) {
            reqBuilder.header("Origin", "https://www.youtube.com")
        }

        httpClient.newCall(reqBuilder.build()).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val bodyStr = resp.body?.string() ?: return null
            val root = JSONObject(bodyStr)
            val streamingData = root.optJSONObject("streamingData") ?: return null

            val bestUrl = pickBestAudioUrl(streamingData) ?: return null
            val expiresAt = parseExpiryMs(bestUrl)
            return ResolvedStream(
                url = bestUrl,
                userAgent = profile.userAgent,
                expiresAtMs = expiresAt,
            )
        }
    }

    private fun pickBestAudioUrl(streamingData: JSONObject): String? {
        val candidates = mutableListOf<Pair<Double, String>>()

        fun collect(arr: JSONArray?, bonusAudioOnly: Double) {
            if (arr == null) return
            for (i in 0 until arr.length()) {
                val fmt = arr.optJSONObject(i) ?: continue
                val url = fmt.optString("url").trim()
                if (url.isEmpty()) continue
                val mime = fmt.optString("mimeType").lowercase()
                val isAudio = mime.startsWith("audio/")
                if (!isAudio && bonusAudioOnly > 0) continue

                val bitrate = fmt.optDouble("averageBitrate", fmt.optDouble("bitrate", 0.0)) / 1000.0
                val codecBonus = when {
                    mime.contains("mp4a") -> 40.0
                    mime.contains("opus") -> 35.0
                    else -> 10.0
                }
                val score = (if (isAudio) 1000.0 else 0.0) + bonusAudioOnly + codecBonus + bitrate
                candidates += score to url
            }
        }

        collect(streamingData.optJSONArray("adaptiveFormats"), 300.0)
        collect(streamingData.optJSONArray("formats"), 0.0)

        candidates.sortByDescending { it.first }
        if (candidates.isNotEmpty()) {
            return candidates.first().second
        }

        val hls = streamingData.optString("hlsManifestUrl").trim()
        return hls.ifEmpty { null }
    }

    private fun parseExpiryMs(url: String): Long {
        val now = System.currentTimeMillis()
        val m = expireRegex.find(url)
        val expSec = m?.groupValues?.getOrNull(1)?.toLongOrNull()
        return if (expSec != null && expSec * 1000L > now + 120_000L) {
            expSec * 1000L - 300_000L
        } else {
            now + 5_400_000L
        }
    }

    fun searchTracks(query: String, limit: Int = 20): List<Track> {
        val cleanQuery = query.trim()
        if (cleanQuery.isEmpty()) return emptyList()

        val ytmResults = runCatching { searchYouTubeMusic(cleanQuery, limit) }.getOrDefault(emptyList())
        if (ytmResults.size >= 5) return ytmResults

        val webResults = runCatching { searchYouTubeWeb(cleanQuery, limit) }.getOrDefault(emptyList())
        return (ytmResults + webResults).distinctBy { it.videoId }.take(limit)
    }

    private fun searchYouTubeMusic(query: String, limit: Int): List<Track> {
        val payload = JSONObject().apply {
            put("query", query)
            put("params", "EgWKAQIIAWoMEAMQBBAJEAoQBRAV")
            put(
                "context",
                JSONObject().put(
                    "client",
                    JSONObject().apply {
                        put("clientName", "WEB_REMIX")
                        put("clientVersion", "1.20250501.01.00")
                        put("hl", "ru")
                        put("gl", "US")
                    }
                )
            )
        }
        val req = Request.Builder()
            .url("https://music.youtube.com/youtubei/v1/search?prettyPrint=false")
            .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
            .header("Origin", "https://music.youtube.com")
            .header("Referer", "https://music.youtube.com/")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .build()

        httpClient.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IOException("YTM search HTTP ${resp.code}")
            val body = resp.body?.string() ?: return emptyList()
            val found = mutableListOf<Track>()
            extractMusicResponsiveItems(JSONObject(body), found, limit)
            return found.distinctBy { it.videoId }.take(limit)
        }
    }

    private fun searchYouTubeWeb(query: String, limit: Int): List<Track> {
        val payload = JSONObject().apply {
            put("query", query)
            put(
                "context",
                JSONObject().put(
                    "client",
                    JSONObject().apply {
                        put("clientName", "WEB")
                        put("clientVersion", "2.20250501.01.00")
                        put("hl", "ru")
                        put("gl", "US")
                    }
                )
            )
        }
        val req = Request.Builder()
            .url("https://www.youtube.com/youtubei/v1/search?prettyPrint=false")
            .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
            .header("Origin", "https://www.youtube.com")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .build()

        httpClient.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw IOException("YT web search HTTP ${resp.code}")
            val body = resp.body?.string() ?: return emptyList()
            val found = mutableListOf<Track>()
            extractVideoRenderers(JSONObject(body), found, limit)
            return found.distinctBy { it.videoId }.take(limit)
        }
    }

    fun fetchRadioMix(seedVideoId: String, limit: Int = 20): List<Track> {
        val payload = JSONObject().apply {
            put("videoId", seedVideoId)
            put("playlistId", "RD$seedVideoId")
            put("isAudioOnly", true)
            put(
                "context",
                JSONObject().put(
                    "client",
                    JSONObject().apply {
                        put("clientName", "WEB_REMIX")
                        put("clientVersion", "1.20250501.01.00")
                        put("hl", "ru")
                        put("gl", "US")
                    }
                )
            )
        }
        val req = Request.Builder()
            .url("https://music.youtube.com/youtubei/v1/next?prettyPrint=false")
            .header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
            .header("Origin", "https://music.youtube.com")
            .header("Referer", "https://music.youtube.com/")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .build()

        return try {
            httpClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return emptyList()
                val body = resp.body?.string() ?: return emptyList()
                val found = mutableListOf<Track>()
                extractPlaylistPanelRenderers(JSONObject(body), found, limit)
                found.distinctBy { it.videoId }.take(limit)
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    private fun extractMusicResponsiveItems(node: Any?, out: MutableList<Track>, limit: Int) {
        if (out.size >= limit || node == null) return
        when (node) {
            is JSONObject -> {
                val renderer = node.optJSONObject("musicResponsiveListItemRenderer")
                if (renderer != null) {
                    parseMusicResponsiveRenderer(renderer)?.let { out += it }
                    return
                }
                val keys = node.keys()
                while (keys.hasNext() && out.size < limit) {
                    extractMusicResponsiveItems(node.opt(keys.next()), out, limit)
                }
            }
            is JSONArray -> {
                for (i in 0 until node.length()) {
                    if (out.size >= limit) break
                    extractMusicResponsiveItems(node.opt(i), out, limit)
                }
            }
        }
    }

    private fun parseMusicResponsiveRenderer(r: JSONObject): Track? {
        val videoId = r.optJSONObject("playlistItemData")?.optString("videoId")?.trim().orEmpty()
            .ifEmpty {
                r.optJSONObject("overlay")
                    ?.optJSONObject("musicItemThumbnailOverlayRenderer")
                    ?.optJSONObject("content")
                    ?.optJSONObject("musicPlayButtonRenderer")
                    ?.optJSONObject("playNavigationEndpoint")
                    ?.optJSONObject("watchEndpoint")
                    ?.optString("videoId")
                    ?.trim()
                    .orEmpty()
            }
        if (videoId.length != 11) return null

        val cols = r.optJSONArray("flexColumns") ?: return null
        val title = readRunsText(
            cols.optJSONObject(0)
                ?.optJSONObject("musicResponsiveListItemFlexColumnRenderer")
                ?.optJSONObject("text")
        ).ifBlank { return null }

        val subtitleRuns = cols.optJSONObject(1)
            ?.optJSONObject("musicResponsiveListItemFlexColumnRenderer")
            ?.optJSONObject("text")
            ?.optJSONArray("runs")

        var artist = ""
        var durationSec = 0L
        if (subtitleRuns != null) {
            val parts = mutableListOf<String>()
            for (i in 0 until subtitleRuns.length()) {
                val txt = subtitleRuns.optJSONObject(i)?.optString("text")?.trim().orEmpty()
                if (txt.isEmpty() || txt == "•" || txt == "·" || txt == "&") continue
                val parsedDur = parseDurationText(txt)
                if (parsedDur > 0L) {
                    durationSec = parsedDur
                } else if (!txt.equals("Song", true) && !txt.equals("Трек", true) && !txt.equals("Video", true)) {
                    parts += txt
                }
            }
            artist = cleanArtist(parts.firstOrNull().orEmpty())
        }

        val thumb = pickThumbnailUrl(
            r.optJSONObject("thumbnail")
                ?.optJSONObject("musicThumbnailRenderer")
                ?.optJSONObject("thumbnail")
                ?.optJSONArray("thumbnails"),
            videoId
        )

        return Track(
            videoId = videoId,
            title = title,
            artist = artist.ifBlank { "Aether Music" },
            durationSec = durationSec,
            thumbnailUrl = thumb,
        )
    }

    private fun extractVideoRenderers(node: Any?, out: MutableList<Track>, limit: Int) {
        if (out.size >= limit || node == null) return
        when (node) {
            is JSONObject -> {
                val vr = node.optJSONObject("videoRenderer")
                if (vr != null) {
                    parseVideoRenderer(vr)?.let { out += it }
                    return
                }
                val keys = node.keys()
                while (keys.hasNext() && out.size < limit) {
                    extractVideoRenderers(node.opt(keys.next()), out, limit)
                }
            }
            is JSONArray -> {
                for (i in 0 until node.length()) {
                    if (out.size >= limit) break
                    extractVideoRenderers(node.opt(i), out, limit)
                }
            }
        }
    }

    private fun parseVideoRenderer(vr: JSONObject): Track? {
        val videoId = vr.optString("videoId").trim()
        if (videoId.length != 11) return null
        val title = readRunsText(vr.optJSONObject("title")).ifBlank { return null }
        val owner = readRunsText(vr.optJSONObject("ownerText"))
            .ifBlank { readRunsText(vr.optJSONObject("longBylineText")) }
        val durationText = vr.optJSONObject("lengthText")?.optString("simpleText")
            ?: readRunsText(vr.optJSONObject("lengthText"))
        val durationSec = parseDurationText(durationText)
        if (durationSec in 1L..24L) return null
        val thumb = pickThumbnailUrl(
            vr.optJSONObject("thumbnail")?.optJSONArray("thumbnails"),
            videoId
        )
        return Track(
            videoId = videoId,
            title = title,
            artist = cleanArtist(owner).ifBlank { "YouTube" },
            durationSec = durationSec,
            thumbnailUrl = thumb,
        )
    }

    private fun extractPlaylistPanelRenderers(node: Any?, out: MutableList<Track>, limit: Int) {
        if (out.size >= limit || node == null) return
        when (node) {
            is JSONObject -> {
                val pvr = node.optJSONObject("playlistPanelVideoRenderer")
                if (pvr != null) {
                    val videoId = pvr.optString("videoId").trim()
                    if (videoId.length == 11) {
                        val title = readRunsText(pvr.optJSONObject("title"))
                        val artist = readRunsText(pvr.optJSONObject("shortBylineText"))
                            .ifBlank { readRunsText(pvr.optJSONObject("longBylineText")) }
                        val durText = pvr.optJSONObject("lengthText")?.optString("simpleText")
                            ?: readRunsText(pvr.optJSONObject("lengthText"))
                        val thumb = pickThumbnailUrl(
                            pvr.optJSONObject("thumbnail")?.optJSONArray("thumbnails"),
                            videoId
                        )
                        if (title.isNotBlank()) {
                            out += Track(
                                videoId = videoId,
                                title = title,
                                artist = cleanArtist(artist).ifBlank { "Aether Music" },
                                durationSec = parseDurationText(durText),
                                thumbnailUrl = thumb,
                            )
                        }
                    }
                    return
                }
                val keys = node.keys()
                while (keys.hasNext() && out.size < limit) {
                    extractPlaylistPanelRenderers(node.opt(keys.next()), out, limit)
                }
            }
            is JSONArray -> {
                for (i in 0 until node.length()) {
                    if (out.size >= limit) break
                    extractPlaylistPanelRenderers(node.opt(i), out, limit)
                }
            }
        }
    }

    private fun readRunsText(obj: JSONObject?): String {
        if (obj == null) return ""
        val simple = obj.optString("simpleText").trim()
        if (simple.isNotEmpty()) return simple
        val runs = obj.optJSONArray("runs") ?: return ""
        val sb = StringBuilder()
        for (i in 0 until runs.length()) {
            sb.append(runs.optJSONObject(i)?.optString("text").orEmpty())
        }
        return sb.toString().trim()
    }

    private fun pickThumbnailUrl(thumbnails: JSONArray?, videoId: String): String {
        if (thumbnails == null || thumbnails.length() == 0) {
            return Track.defaultThumbnail(videoId)
        }
        var bestUrl: String? = null
        var bestPenalty = Int.MAX_VALUE
        for (i in 0 until thumbnails.length()) {
            val item = thumbnails.optJSONObject(i) ?: continue
            val url = item.optString("url").trim()
            if (url.isEmpty()) continue
            val dim = maxOf(item.optInt("width", 0), item.optInt("height", 0))
            val penalty = kotlin.math.abs((if (dim > 0) dim else 360) - 480)
            if (penalty < bestPenalty) {
                bestPenalty = penalty
                bestUrl = url
            }
        }
        return bestUrl ?: Track.defaultThumbnail(videoId)
    }

    private fun cleanArtist(raw: String): String =
        artistSuffixRegex.replace(raw, "").trim()

    private fun parseDurationText(raw: String?): Long {
        if (raw.isNullOrBlank()) return 0L
        val parts = raw.trim().split(":").mapNotNull { it.toLongOrNull() }
        return when (parts.size) {
            2 -> parts[0] * 60L + parts[1]
            3 -> parts[0] * 3600L + parts[1] * 60L + parts[2]
            else -> 0L
        }
    }

    companion object {
        private val newPipeInitialized = AtomicBoolean(false)

        fun ensureNewPipeInitialized(client: OkHttpClient) {
            if (newPipeInitialized.compareAndSet(false, true)) {
                try {
                    NewPipe.init(
                        OkHttpNewPipeDownloader(client),
                        Localization("ru", "RU"),
                        ContentCountry("US"),
                    )
                } catch (_: Exception) {
                    newPipeInitialized.set(false)
                }
            }
        }
    }
}
