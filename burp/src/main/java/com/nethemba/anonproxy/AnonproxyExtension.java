package com.nethemba.anonproxy;

import burp.api.montoya.BurpExtension;
import burp.api.montoya.MontoyaApi;
import burp.api.montoya.http.handler.*;
import burp.api.montoya.http.message.HttpHeader;
import burp.api.montoya.http.message.requests.HttpRequest;
import burp.api.montoya.http.message.responses.HttpResponse;
import burp.api.montoya.logging.Logging;
import burp.api.montoya.ui.contextmenu.ContextMenuEvent;
import burp.api.montoya.ui.contextmenu.ContextMenuItemsProvider;

import javax.swing.JMenuItem;
import java.awt.Component;
import java.awt.Toolkit;
import java.awt.datatransfer.StringSelection;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpResponse.BodyHandlers;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

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
public class AnonproxyExtension implements BurpExtension, HttpHandler, ContextMenuItemsProvider {

    // Where the Python engine API listens (python -m anonproxy serve).
    // Precedence: JVM system property > environment variable > default.
    // System properties matter because Burp launched from the Dock does not
    // inherit your shell environment (-Danonproxy.engagement=acme-2026).
    private static String cfg(String prop, String envKey, String dflt) {
        String v = System.getProperty(prop);
        if (v != null && !v.isBlank()) return v.trim();
        v = System.getenv(envKey);
        if (v != null && !v.isBlank()) return v.trim();
        return dflt;
    }

    private static final String ENGINE = cfg(
            "anonproxy.engine", "ANONPROXY_ENGINE", "http://127.0.0.1:8099");
    private static final String TOKEN = cfg(
            "anonproxy.token", "ANONPROXY_API_TOKEN", "");

    // Engagement resolution: an explicit setting (system property / env) wins;
    // otherwise the extension FOLLOWS THE ENGINE — /anonproxy/health reports
    // the engagement the menubar started, so Burp always shares that vault
    // instead of silently writing to "default" while the engine runs
    // "acme-2026". "default" remains the last-resort fallback.
    private String engagement;

    private MontoyaApi api;
    private Logging log;
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            // Burp sets JVM-wide proxy properties; without this the
            // extension's engine calls get routed through Burp's own
            // listener and die there. The engine is localhost — never proxy.
            .proxy(HttpClient.Builder.NO_PROXY)
            // Java defaults to HTTP/2, which sends `Upgrade: h2c` on
            // cleartext POSTs; uvicorn rejects the upgrade and drops the
            // body (500 on an empty JSON payload). Speak HTTP/1.1 only.
            .version(HttpClient.Version.HTTP_1_1)
            .build();

    // messageId() is identical between a request and its response (confirmed in
    // the Montoya javadoc). Tracking it here is more reliable than re-checking
    // for X-Anonproxy on response.initiatingRequest() — that header is removed
    // in handleHttpRequestToBeSent before the request is sent, and it was never
    // confirmed whether initiatingRequest() reflects the pre- or post-modification
    // request. This sidesteps the question entirely.
    private final Set<Integer> anonymizedMessageIds = ConcurrentHashMap.newKeySet();

    @Override
    public void initialize(MontoyaApi api) {
        this.api = api;
        this.log = api.logging();
        api.extension().setName("Anonproxy");
        api.http().registerHttpHandler(this);
        api.userInterface().registerContextMenuItemsProvider(this);
        this.engagement = resolveEngagement();
        log.logToOutput("Anonproxy loaded. Engine=" + ENGINE + " engagement=" + engagement);
    }

    private String resolveEngagement() {
        String explicit = cfg("anonproxy.engagement", "ENGAGEMENT_ID", "");
        if (!explicit.isEmpty()) {
            log.logToOutput("engagement " + explicit + " (explicit override)");
            return explicit;
        }
        try {
            var req = java.net.http.HttpRequest.newBuilder()
                    .uri(URI.create(ENGINE + "/anonproxy/health"))
                    .timeout(Duration.ofSeconds(3))
                    .GET().build();
            var resp = http.send(req, BodyHandlers.ofString());
            String body = resp.body();
            if (resp.statusCode() == 200 && body != null) {
                String eng = extractEngagement(body);
                if (eng != null && !eng.isEmpty()) {
                    log.logToOutput("engagement " + eng + " (follows the engine)");
                    return eng;
                }
            }
            log.logToError("engine did not report an engagement (HTTP "
                    + resp.statusCode() + ") — falling back to 'default'");
        } catch (Exception e) {
            log.logToError("engine unreachable while resolving engagement ("
                    + e.getMessage() + ") — falling back to 'default'");
        }
        return "default";
    }

    /** Pull "engagement":"..." out of the health payload without a JSON dep. */
    private static String extractEngagement(String body) {
        int k = body.indexOf("\"engagement\"");
        if (k < 0) return null;
        int colon = body.indexOf(':', k);
        int q1 = body.indexOf('"', colon);
        int q2 = body.indexOf('"', q1 + 1);
        return (q1 >= 0 && q2 > q1) ? body.substring(q1 + 1, q2) : null;
    }

    @Override
    public RequestToBeSentAction handleHttpRequestToBeSent(HttpRequestToBeSent request) {
        // Only touch traffic from Repeater/Intruder/Proxy when explicitly enabled
        // by header X-Anonproxy: anon. Keeps unrelated traffic untouched.
        if (!request.hasHeader("X-Anonproxy")) {
            return RequestToBeSentAction.continueWith(request);
        }
        anonymizedMessageIds.add(request.messageId());
        HttpRequest updated = request.withRemovedHeader("X-Anonproxy");

        // Only the Cookie header is touched. Host/Authorization are left alone
        // on purpose: for the documented workflow (a Repeater request TO an LLM
        // endpoint), Host is the LLM provider's own domain and Authorization is
        // the operator's own API key — anonymizing either breaks delivery. A
        // leftover Cookie header from a "Send to Repeater"'d target request,
        // however, is exactly the kind of accidental real-session-token leak
        // this whole tool exists to prevent, and cookies are never required for
        // reaching an LLM API.
        String cookie = updated.headerValue("Cookie");
        if (cookie != null && !cookie.isEmpty()) {
            // the engine's cookie-value regex is anchored on the full header
            // line ("Cookie: ..."), because that's the shape it sees when
            // scanning raw HTTP text everywhere else — headerValue() gives us
            // only the value, so wrap the label back on, then strip it off.
            String withLabel = call("/anonproxy/anonymize", "Cookie: " + cookie, true);
            if (withLabel != null && withLabel.regionMatches(true, 0, "Cookie: ", 0, 8)) {
                updated = updated.withHeader("Cookie", withLabel.substring(8));
            } else {
                log.logToError("Anonproxy engine unreachable or returned unexpected shape "
                        + "— Cookie header left unchanged");
            }
        }

        String body = updated.bodyToString();
        if (body != null && !body.isEmpty()) {
            String anon = call("/anonproxy/anonymize", body, true);
            if (anon == null) {
                log.logToError("Anonproxy engine unreachable — body left unchanged");
            } else {
                updated = updated.withBody(anon);
            }
        }
        return RequestToBeSentAction.continueWith(updated);
    }

    @Override
    public ResponseReceivedAction handleHttpResponseReceived(HttpResponseReceived response) {
        // Deanonymize responses for the same opt-in traffic. Keyed on messageId
        // (identical between a request and its response, per the Montoya
        // javadoc), not on a header surviving into initiatingRequest().
        if (!anonymizedMessageIds.remove(response.messageId())) {
            return ResponseReceivedAction.continueWith(response);
        }

        HttpResponse updated = response;
        // A response can carry multiple Set-Cookie headers (one per cookie).
        // withRemovedHeader(HttpHeader) removes by NAME, not by the specific
        // instance — confirmed live against a real two-Set-Cookie response,
        // where a remove-then-add loop silently dropped every header but the
        // last one processed. Fix: compute every replacement first (without
        // mutating anything), then do exactly one bulk remove + one bulk add.
        List<HttpHeader> setCookieHeaders = updated.headers().stream()
                .filter(h -> h.name().equalsIgnoreCase("Set-Cookie"))
                .toList();
        if (!setCookieHeaders.isEmpty()) {
            List<HttpHeader> replacements = new ArrayList<>();
            for (HttpHeader h : setCookieHeaders) {
                // same label-wrapping reason as the request-side Cookie header above
                String withLabel = call("/anonproxy/anonymize", "Set-Cookie: " + h.value(), true);
                if (withLabel == null || !withLabel.regionMatches(true, 0, "Set-Cookie: ", 0, 12)) {
                    log.logToError("Anonproxy engine unreachable or returned unexpected shape "
                            + "— one Set-Cookie header left unchanged");
                    replacements.add(h);  // keep the original rather than drop it
                } else {
                    replacements.add(HttpHeader.httpHeader(h.name(), withLabel.substring(12)));
                }
            }
            updated = updated.withRemovedHeader("Set-Cookie").withAddedHeaders(replacements);
        }

        String body = updated.bodyToString();
        if (body == null || body.isEmpty()) {
            return ResponseReceivedAction.continueWith(updated);
        }
        String real = call("/anonproxy/deanonymize", body, false);
        if (real == null) {
            log.logToError("Anonproxy engine unreachable — response body left unchanged");
            return ResponseReceivedAction.continueWith(updated);
        }
        return ResponseReceivedAction.continueWith(updated.withBody(real));
    }

    /** POST {text, engagement, is_tool_output} to the engine; return result or null. */
    private String call(String path, String text, boolean isToolOutput) {
        try {
            String payload = "{"
                    + "\"text\":" + jsonString(text) + ","
                    + "\"engagement\":" + jsonString(engagement) + ","
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

    // --- context menu: copy anonymized to clipboard --------------------------
    // The Burp-MCP workflow is already covered by the proxy itself (tool
    // results flowing through :8099 get anonymized there). This menu covers
    // everything else: select any request/response in Proxy history, Repeater
    // or an editor, right-click, and the full HTTP text lands on the
    // clipboard with real values replaced by engagement surrogates — paste
    // into claude.ai, ChatGPT, a ticket, wherever.

    @Override
    public List<Component> provideMenuItems(ContextMenuEvent event) {
        boolean hasTargets = !event.selectedRequestResponses().isEmpty()
                || event.messageEditorRequestResponse().isPresent();
        if (!hasTargets) {
            return List.of();
        }
        JMenuItem item = new JMenuItem("Anonproxy: Copy anonymized to clipboard");
        item.addActionListener(e -> copyAnonymized(event));
        return List.of(item);
    }

    private void copyAnonymized(ContextMenuEvent event) {
        int items = 0;
        if (event.messageEditorRequestResponse().isPresent()) {
            var mer = event.messageEditorRequestResponse().get();
            if (mer.requestResponse().request() != null) {
                copyAnonymize(mer.requestResponse().request().toString());
                items++;
            }
            if (mer.requestResponse().response() != null) {
                copyAnonymize(mer.requestResponse().response().toString());
                items++;
            }
        }
        for (var rr : event.selectedRequestResponses()) {
            copyAnonymize(rr.request().toString());
            if (rr.response() != null) {
                copyAnonymize(rr.response().toString());
            }
            items++;
        }
        log.logToOutput("copied " + items + " anonymized item(s) to clipboard "
                + "(engagement=" + engagement + ")");
    }

    private void copyAnonymize(String httpText) {
        String anon = call("/anonproxy/anonymize", httpText, true);
        String result;
        if (anon != null) {
            result = anon;
        } else {
            // engine unreachable: fail CLOSED — never place unredacted
            // traffic on a clipboard under an "anonymized" label.
            log.logToError("clipboard NOT populated (engine error) — see Errors tab");
            result = "[ANONPROXY: engine error — nothing copied]";
        }
        var selection = new StringSelection(result);
        Toolkit.getDefaultToolkit().getSystemClipboard().setContents(selection, null);
    }
}
