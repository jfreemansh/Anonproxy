package com.nethemba.anonproxy;

import burp.api.montoya.BurpExtension;
import burp.api.montoya.MontoyaApi;
import burp.api.montoya.http.handler.*;
import burp.api.montoya.http.message.requests.HttpRequest;
import burp.api.montoya.http.message.responses.HttpResponse;
import burp.api.montoya.logging.Logging;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpResponse.BodyHandlers;
import java.nio.charset.StandardCharsets;
import java.time.Duration;

/**
 * Anonproxy for Burp Suite.
 *
 * Rather than re-implementing detection in Java (and drifting from the proxy's
 * behaviour), this extension delegates to the same Anonproxy engine over its
 * local HTTP API.  That means Burp, Claude Code, and the OpenAI SDK all share
 * one vault and produce identical, consistent surrogates within an engagement —
 * and you get the tolerant restorer for free on responses.
 *
 * Direction of transformation (configurable):
 *   - Requests  leaving Burp to an LLM endpoint -> anonymize  (real -> surrogate)
 *   - Responses coming back                      -> deanonymize (surrogate -> real)
 *
 * This is far more reliable than Burp's built-in Match/Replace rules, which are
 * literal-string only: they miss anything they weren't pre-seeded with and can't
 * restore a surrogate the model reformatted.
 */
public class AnonproxyExtension implements BurpExtension, HttpHandler {

    // Where the Python engine API listens (python -m anonproxy serve).
    private static final String ENGINE = System.getenv().getOrDefault(
            "ANONPROXY_ENGINE", "http://127.0.0.1:8080");
    private static final String ENGAGEMENT = System.getenv().getOrDefault(
            "ENGAGEMENT_ID", "default");
    private static final String TOKEN = System.getenv().getOrDefault(
            "ANONPROXY_API_TOKEN", "");

    private MontoyaApi api;
    private Logging log;
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5)).build();

    @Override
    public void initialize(MontoyaApi api) {
        this.api = api;
        this.log = api.logging();
        api.extension().setName("Anonproxy (Nethemba)");
        api.http().registerHttpHandler(this);
        log.logToOutput("Anonproxy loaded. Engine=" + ENGINE + " engagement=" + ENGAGEMENT);
    }

    @Override
    public RequestToBeSentAction handleHttpRequestToBeSent(HttpRequestToBeSent request) {
        // Only touch traffic from Repeater/Intruder/Proxy when explicitly enabled
        // by header X-Anonproxy: anon. Keeps unrelated traffic untouched.
        if (!request.hasHeader("X-Anonproxy")) {
            return RequestToBeSentAction.continueWith(request);
        }
        String body = request.bodyToString();
        if (body == null || body.isEmpty()) {
            return RequestToBeSentAction.continueWith(request);
        }
        String anon = call("/anonproxy/anonymize", body, true);
        if (anon == null) {
            log.logToError("Anonproxy engine unreachable — passing request unchanged");
            return RequestToBeSentAction.continueWith(request);
        }
        HttpRequest updated = request.withBody(anon).withRemovedHeader("X-Anonproxy");
        return RequestToBeSentAction.continueWith(updated);
    }

    @Override
    public ResponseReceivedAction handleHttpResponseReceived(HttpResponseReceived response) {
        // Deanonymize responses for the same opt-in traffic.
        if (response.initiatingRequest() == null
                || !response.initiatingRequest().hasHeader("X-Anonproxy")) {
            return ResponseReceivedAction.continueWith(response);
        }
        String body = response.bodyToString();
        if (body == null || body.isEmpty()) {
            return ResponseReceivedAction.continueWith(response);
        }
        String real = call("/anonproxy/deanonymize", body, false);
        if (real == null) {
            return ResponseReceivedAction.continueWith(response);
        }
        HttpResponse updated = response.withBody(real);
        return ResponseReceivedAction.continueWith(updated);
    }

    /** POST {text, engagement, is_tool_output} to the engine; return result or null. */
    private String call(String path, String text, boolean isToolOutput) {
        try {
            String payload = "{"
                    + "\"text\":" + jsonString(text) + ","
                    + "\"engagement\":" + jsonString(ENGAGEMENT) + ","
                    + "\"is_tool_output\":" + isToolOutput
                    + "}";
            var builder = java.net.http.HttpRequest.newBuilder()
                    .uri(URI.create(ENGINE + path))
                    .timeout(Duration.ofSeconds(120))
                    .header("Content-Type", "application/json");
            if (!TOKEN.isEmpty()) {
                builder.header("X-Anonproxy-Token", TOKEN);
            }
            var req = builder.POST(
                    java.net.http.HttpRequest.BodyPublishers.ofString(payload, StandardCharsets.UTF_8))
                    .build();
            var resp = http.send(req, BodyHandlers.ofString());
            if (resp.statusCode() != 200) {
                log.logToError("engine returned " + resp.statusCode());
                return null;
            }
            return extractResult(resp.body());
        } catch (Exception e) {
            log.logToError("engine call failed: " + e.getMessage());
            return null;
        }
    }

    // --- tiny JSON helpers (avoid pulling in a JSON dependency) --------------
    private static String jsonString(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"':  b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n");  break;
                case '\r': b.append("\\r");  break;
                case '\t': b.append("\\t");  break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    /** Pull the "result" string value out of {"result": "..."} without a JSON lib. */
    private static String extractResult(String json) {
        String key = "\"result\":";
        int i = json.indexOf(key);
        if (i < 0) return null;
        int j = json.indexOf('"', i + key.length());
        if (j < 0) return null;
        StringBuilder out = new StringBuilder();
        for (int k = j + 1; k < json.length(); k++) {
            char c = json.charAt(k);
            if (c == '\\' && k + 1 < json.length()) {
                char n = json.charAt(++k);
                switch (n) {
                    case 'n': out.append('\n'); break;
                    case 'r': out.append('\r'); break;
                    case 't': out.append('\t'); break;
                    case '"': out.append('"'); break;
                    case '\\': out.append('\\'); break;
                    case 'u':
                        out.append((char) Integer.parseInt(json.substring(k + 1, k + 5), 16));
                        k += 4; break;
                    default: out.append(n);
                }
            } else if (c == '"') {
                break;
            } else {
                out.append(c);
            }
        }
        return out.toString();
    }
}
