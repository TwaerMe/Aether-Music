package com.aether.music.data

import okhttp3.OkHttpClient
import okhttp3.RequestBody.Companion.toRequestBody
import org.schabi.newpipe.extractor.downloader.Downloader
import org.schabi.newpipe.extractor.downloader.Request
import org.schabi.newpipe.extractor.downloader.Response
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException
import java.io.IOException

class OkHttpNewPipeDownloader(
    private val client: OkHttpClient,
) : Downloader() {

    @Throws(IOException::class, ReCaptchaException::class)
    override fun execute(request: Request): Response {
        val httpMethod = request.httpMethod()
        val url = request.url()
        val headers = request.headers()
        val dataToSend = request.dataToSend()

        val requestBody = if (dataToSend != null) {
            dataToSend.toRequestBody(null, 0, dataToSend.size)
        } else if (httpMethod.equals("POST", ignoreCase = true) || httpMethod.equals("PUT", ignoreCase = true)) {
            ByteArray(0).toRequestBody(null, 0, 0)
        } else {
            null
        }

        val reqBuilder = okhttp3.Request.Builder()
            .method(httpMethod, requestBody)
            .url(url)
            .header("User-Agent", DEFAULT_USER_AGENT)

        for ((headerName, headerValues) in headers) {
            if (headerValues.size > 1) {
                reqBuilder.removeHeader(headerName)
                for (value in headerValues) {
                    reqBuilder.addHeader(headerName, value)
                }
            } else if (headerValues.size == 1) {
                reqBuilder.header(headerName, headerValues[0])
            }
        }

        client.newCall(reqBuilder.build()).execute().use { resp ->
            if (resp.code == 429) {
                throw ReCaptchaException("reCaptcha Challenge requested", url)
            }
            val bodyString = resp.body?.string()
            val latestUrl = resp.request.url.toString()
            return Response(
                resp.code,
                resp.message,
                resp.headers.toMultimap(),
                bodyString,
                latestUrl,
            )
        }
    }

    companion object {
        const val DEFAULT_USER_AGENT =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0"
    }
}
