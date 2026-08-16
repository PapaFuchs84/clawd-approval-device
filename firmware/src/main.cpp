// Clawd Approval Device firmware
// Authenticated WebSocket client to the host daemon, HMAC-signed button
// events, animated status display. See ../../docs/PROTOCOL.md for the wire
// protocol and ../../docs/HARDWARE.md for board identification.

#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>
#include <OneButton.h>
#include <mbedtls/md.h>
#include <math.h>
#include "secrets.h"
#include "sprites.h"

TFT_eSPI tft = TFT_eSPI();
WebSocketsClient webSocket;
OneButton btnApprove(PIN_BUTTON_APPROVE, true);
OneButton btnDeny(PIN_BUTTON_DENY, true);

String currentRequestId = "";
bool wsConnected = false;
uint32_t eventCounter = 0;

static void drawStatus(const String &line1, const String &line2) {
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(2);
  tft.setCursor(4, 10);
  tft.println(line1);
  tft.setTextSize(1);
  tft.setCursor(4, 40);
  tft.println(line2);
}

// --- Graphical state display (Clawd mascot, sprites from sprites.h) ---
// Sprite source: marciogranzotto/clawd-tank, assets/svg-animations/,
// statically rendered via tools/svg_to_rgb565.py (see comment there).
// MIT-licensed, see THIRD_PARTY_NOTICES.md.

#define COLOR_IDLE         0x39E7  // dark blue-grey
#define COLOR_WORKING      0x059F  // blue
#define COLOR_WAITING      0xFD00  // amber
#define COLOR_SUCCESS      0x0720  // green
#define COLOR_ERROR        TFT_RED
#define COLOR_DISCONNECTED 0x7BEF  // grey

// gfx: TFT_eSPI or TFT_eSprite (TFT_eSprite publicly inherits from TFT_eSPI
// and shares the same drawing API), so this same function can draw either
// straight to the display or into an off-screen buffer.
static void drawBadge(TFT_eSPI &gfx, int cx, int cy, char type, uint16_t color, int radiusOffset = 0) {
  int r = 9 + radiusOffset;
  gfx.fillCircle(cx, cy, r, color);
  gfx.drawCircle(cx, cy, r, TFT_BLACK);
  if (type == '!') {
    gfx.fillRect(cx - 1, cy - 5, 2, 6, TFT_BLACK);
    gfx.fillRect(cx - 1, cy + 3, 2, 2, TFT_BLACK);
  } else if (type == 'v') {
    gfx.drawLine(cx - 4, cy, cx - 1, cy + 3, TFT_BLACK);
    gfx.drawLine(cx - 1, cy + 3, cx + 4, cy - 3, TFT_BLACK);
    gfx.drawLine(cx - 4, cy + 1, cx - 1, cy + 4, TFT_BLACK);
    gfx.drawLine(cx - 1, cy + 4, cx + 4, cy - 2, TFT_BLACK);
  } else if (type == 'x') {
    gfx.drawLine(cx - 4, cy - 4, cx + 4, cy + 4, TFT_BLACK);
    gfx.drawLine(cx - 4, cy + 4, cx + 4, cy - 4, TFT_BLACK);
    gfx.drawLine(cx - 4, cy - 3, cx + 3, cy + 4, TFT_BLACK);
    gfx.drawLine(cx - 4, cy + 3, cx + 3, cy - 4, TFT_BLACK);
  }
}

// Simple word wrap for the summary column next to the sprite.
static void drawWrapped(const String &text, int x, int y, int maxWidth, int lineHeight, int maxY) {
  int start = 0;
  int curY = y;
  int len = text.length();
  while (start < len && curY <= maxY) {
    int end = start;
    int lastSpace = -1;
    while (end < len) {
      if (text[end] == ' ') lastSpace = end;
      if (tft.textWidth(text.substring(start, end + 1)) > maxWidth) {
        if (lastSpace > start) end = lastSpace;
        break;
      }
      end++;
    }
    if (end > len) end = len;
    tft.setCursor(x, curY);
    tft.print(text.substring(start, end));
    curY += lineHeight;
    start = end;
    while (start < len && text[start] == ' ') start++;
  }
}

// --- Animation state (global, redrawn periodically by animateTick()) ---
const int SPRITE_BASE_X = 6, SPRITE_BASE_Y = 30;
const int SPRITE_PAD = 10;  // margin for shake/bounce so nothing gets clipped

String activeState = "IDLE";
const uint16_t *activeSprite = sprite_IDLE;
char activeBadge = 0;
uint16_t activeBadgeColor = TFT_BLACK;
uint32_t animStartMs = 0;
uint32_t lastAnimDrawMs = 0;

// Off-screen buffer for the animation area: composited in RAM and pushed to
// the display in a SINGLE SPI transfer, instead of clear+redraw as two
// separate operations (which causes visible flicker, since the screen briefly
// shows the cleared state in between).
TFT_eSprite animSprite = TFT_eSprite(&tft);
const int ANIM_W = SPRITE_SIZE + 2 * SPRITE_PAD;
const int ANIM_H = SPRITE_SIZE + 2 * SPRITE_PAD;

// Redraws ONLY the sprite area with the state-dependent animation. Called
// immediately on every state change and then every ~80ms from loop().
static void animateTick() {
  uint32_t t = millis() - animStartMs;
  int offX = 0, offY = 0;
  bool showBadge = (activeBadge != 0);
  int badgePulse = 0;

  if (activeState == "IDLE") {
    offY = (int)roundf(sinf(t / 900.0f) * 2.0f);            // calm breathing
  } else if (activeState == "WORKING") {
    offY = (int)roundf(sinf(t / 260.0f) * 2.0f);             // lively bobbing
  } else if (activeState == "WAITING_APPROVAL") {
    offY = (int)roundf(sinf(t / 900.0f) * 2.0f);
    badgePulse = (int)roundf((sinf(t / 260.0f) + 1.0f) * 1.5f);  // pulsing badge
  } else if (activeState == "SUCCESS") {
    offY = (int)roundf(fabsf(sinf(t / 220.0f)) * -4.0f);     // little happy hops
  } else if (activeState == "ERROR") {
    offX = (int)roundf(sinf(t / 70.0f) * 3.0f);              // jittery shake
  } else if (activeState == "DISCONNECTED") {
    offY = (int)roundf(sinf(t / 1400.0f) * 1.0f);
    showBadge = showBadge && (((t / 500) % 2) == 0);         // blinking badge
  }

  animSprite.fillSprite(TFT_BLACK);
  animSprite.pushImage(SPRITE_PAD + offX, SPRITE_PAD + offY, SPRITE_SIZE, SPRITE_SIZE, activeSprite);
  if (showBadge) {
    drawBadge(animSprite, SPRITE_PAD + offX + SPRITE_SIZE - 8, SPRITE_PAD + offY + SPRITE_SIZE - 8,
              activeBadge, activeBadgeColor, badgePulse);
  }
  animSprite.pushSprite(SPRITE_BASE_X - SPRITE_PAD, SPRITE_BASE_Y - SPRITE_PAD);
}

static void renderClawdState(const String &state, const String &summary) {
  uint16_t barColor = COLOR_IDLE;
  const char *label = "IDLE";

  activeState = state;
  activeSprite = sprite_IDLE;
  activeBadge = 0;
  activeBadgeColor = TFT_BLACK;
  animStartMs = millis();

  if (state == "WORKING") {
    activeSprite = sprite_WORKING; barColor = COLOR_WORKING; label = "WORKING";
  } else if (state == "WAITING_APPROVAL") {
    activeSprite = sprite_WAITING_APPROVAL; barColor = COLOR_WAITING; label = "CONFIRM?";
    activeBadge = '!'; activeBadgeColor = COLOR_WAITING;
  } else if (state == "SUCCESS") {
    activeSprite = sprite_SUCCESS; barColor = COLOR_SUCCESS; label = "DONE";
    activeBadge = 'v'; activeBadgeColor = COLOR_SUCCESS;
  } else if (state == "ERROR") {
    activeSprite = sprite_ERROR; barColor = COLOR_ERROR; label = "ERROR";
  } else if (state == "DISCONNECTED") {
    activeSprite = sprite_DISCONNECTED; barColor = COLOR_DISCONNECTED; label = "OFFLINE";
    activeBadge = 'x'; activeBadgeColor = COLOR_DISCONNECTED;
  }

  tft.fillScreen(TFT_BLACK);

  // Status bar on top, color = state (readable from across the room)
  tft.fillRect(0, 0, 240, 22, barColor);
  tft.setTextColor(TFT_BLACK, barColor);
  tft.setTextSize(2);
  tft.setCursor(6, 4);
  tft.print(label);

  // Summary text on the right, word-wrapped
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextSize(1);
  const int textX = SPRITE_BASE_X + SPRITE_SIZE + 8;
  drawWrapped(summary, textX, 32, 240 - textX - 4, 12, 128);

  animateTick();  // draw the first frame immediately, don't wait 80ms
}

// HMAC-SHA256 over the canonical JSON representation (fields sorted, exactly
// as produced by the daemon: json.dumps(body, sort_keys=True)).
static String hmacHex(const String &canonicalJson) {
  byte hmacResult[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char *)HMAC_KEY, strlen(HMAC_KEY));
  mbedtls_md_hmac_update(&ctx, (const unsigned char *)canonicalJson.c_str(), canonicalJson.length());
  mbedtls_md_hmac_finish(&ctx, hmacResult);
  mbedtls_md_free(&ctx);

  String hex = "";
  for (int i = 0; i < 32; i++) {
    char buf[3];
    sprintf(buf, "%02x", hmacResult[i]);
    hex += buf;
  }
  return hex;
}

// Generates a random event ID (for duplicate detection on the daemon side).
static String genEventId() {
  eventCounter++;
  return "evt_" + String((uint32_t)millis(), HEX) + "_" + String(eventCounter);
}

static void sendButtonEvent(const char *button) {
  if (!wsConnected) {
    Serial.println("[WARN] No WS connection, cannot send button event");
    return;
  }
  if (currentRequestId.isEmpty()) {
    Serial.println("[INFO] No pending request, button event not sent");
    return;
  }

  String eventId = genEventId();
  uint32_t ts = millis() / 1000;

  // Canonical JSON MUST exactly match the field order/formatting the daemon
  // produces with json.dumps(sort_keys=True): alphabetical order, no spaces
  // after ':' or ',' -> ArduinoJson with compact serializeJson() + keys
  // inserted in alphabetical order.
  StaticJsonDocument<256> doc;
  doc["button"] = button;
  doc["event_id"] = eventId;
  doc["request_id"] = currentRequestId;
  doc["ts"] = ts;
  doc["type"] = "button_event";
  String canonical;
  serializeJson(doc, canonical);

  String sig = hmacHex(canonical);
  doc["sig"] = sig;
  String full;
  serializeJson(doc, full);

  webSocket.sendTXT(full);
  Serial.printf("[SENT] %s\n", full.c_str());
}

static void onApprovePress() { sendButtonEvent("approve"); }
static void onDenyPress()    { sendButtonEvent("deny"); }

static void handleStateMessage(JsonDocument &doc) {
  const char *state = doc["state"] | "";
  const char *reqId = doc["request_id"] | "";
  const char *summary = doc["summary"] | "";
  const char *msgId = doc["msg_id"] | "";

  currentRequestId = String(reqId);

  Serial.printf("[STATE] %s request_id=%s summary=%s\n", state, reqId, summary);
  renderClawdState(String(state), String(summary));

  // Send ACK back
  StaticJsonDocument<128> ack;
  ack["type"] = "ack";
  ack["msg_id"] = msgId;
  String ackStr;
  serializeJson(ack, ackStr);
  webSocket.sendTXT(ackStr);
}

static void webSocketEvent(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_DISCONNECTED:
      wsConnected = false;
      currentRequestId = "";
      Serial.println("[WS] Disconnected");
      renderClawdState("DISCONNECTED", "Daemon unreachable");
      break;

    case WStype_CONNECTED:
      wsConnected = true;
      Serial.println("[WS] Connected (authenticated)");
      renderClawdState("IDLE", "Daemon connected");
      break;

    case WStype_TEXT: {
      StaticJsonDocument<512> doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) {
        Serial.printf("[WARN] JSON parse error: %s\n", err.c_str());
        return;
      }
      const char *mtype = doc["type"] | "";
      if (strcmp(mtype, "state") == 0) {
        handleStateMessage(doc);
      } else {
        Serial.printf("[WS] Unknown message type: %s\n", mtype);
      }
      break;
    }

    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== Clawd Approval Device ===");

  tft.init();
  tft.setRotation(1);
  tft.setSwapBytes(true);  // RGB565 sprites in sprites.h are in host byte order

  animSprite.setColorDepth(16);
  animSprite.createSprite(ANIM_W, ANIM_H);
  animSprite.setSwapBytes(true);
  drawStatus("Booting...", "");

  btnApprove.attachClick(onApprovePress);
  btnDeny.attachClick(onDenyPress);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  drawStatus("Connecting WiFi...", WIFI_SSID);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
    Serial.print(".");
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nWiFi connection failed (timeout)");
    drawStatus("WIFI ERROR", "Timeout after 15s");
    return;
  }

  Serial.printf("\nWiFi connected, IP: %s\n", WiFi.localIP().toString().c_str());
  drawStatus("WIFI OK", WiFi.localIP().toString());
  delay(1000);

  // NO trailing "\r\n" here: WebSocketsClient::sendHeader() already appends
  // its own "\r\n" after extraHeaders (see WebSocketsClient.cpp:742). An
  // extra "\r\n" produces a double CRLF that terminates the HTTP header
  // block early, making the following User-Agent header appear as an
  // (invalid) WS frame -> server aborts with code 1002 (protocol error).
  String authHeader = "Authorization: Bearer " + String(AUTH_TOKEN);
  webSocket.setExtraHeaders(authHeader.c_str());
  webSocket.begin(DAEMON_WS_HOST, DAEMON_WS_PORT, "/");
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(3000);

  drawStatus("Connecting daemon...", String(DAEMON_WS_HOST));
  Serial.printf("Connecting to ws://%s:%d/\n", DAEMON_WS_HOST, DAEMON_WS_PORT);
}

void loop() {
  webSocket.loop();
  btnApprove.tick();
  btnDeny.tick();

  uint32_t now = millis();
  if (now - lastAnimDrawMs >= 80) {  // ~12 fps, smooth enough for these small movements
    lastAnimDrawMs = now;
    animateTick();
  }
}
