/*
 * SonicGrid - nod acustic ESP32 + INMP441
 *
 * Citeste microfonul prin I2S, calculeaza nivelul in dBFS si trimite
 * o citire pe secunda catre serverul Flask de pe Raspberry Pi.
 *
 * IMPORTANT: schimba NODE_ID pentru fiecare placa (n1, n2, n3, n4).
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <driver/i2s.h>

// ---------------------------------------------------------------- configurare

#define NODE_ID     "n1"          // <-- UNIC pentru fiecare placa!

const char* WIFI_SSID = "sonicnet";
const char* WIFI_PASS = "parola_ta";

// Numele mDNS al Pi-ului. Daca mDNS nu merge, pune IP-ul direct:
// const char* SERVER_URL = "http://172.20.10.5:5000/api/ingest";
const char* SERVER_URL = "http://sonicgrid.local:5000/api/ingest";

const unsigned long SEND_INTERVAL = 1000;   // ms intre doua trimiteri

// Pini I2S (aceiasi ca in testul care a mers)
#define I2S_SCK  14
#define I2S_WS   15
#define I2S_SD   32

#define I2S_PORT      I2S_NUM_0
#define SAMPLE_RATE   16000
#define BUF_SAMPLES   512

int32_t samples[BUF_SAMPLES];

// Acumulatori pentru fereastra de o secunda
double  sumSquares  = 0;
uint32_t sampleCount = 0;
int32_t maxPeakToPeak = 0;
unsigned long lastSend = 0;

// ---------------------------------------------------------------- I2S

void i2sInit() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = BUF_SAMPLES,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0
  };

  i2s_pin_config_t pins = {
    .bck_io_num   = I2S_SCK,
    .ws_io_num    = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = I2S_SD
  };

  i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
  i2s_set_pin(I2S_PORT, &pins);
  i2s_zero_dma_buffer(I2S_PORT);
}

// ---------------------------------------------------------------- WiFi

void wifiConnect() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);              // fara asta, latenta creste imprevizibil
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.printf("Ma conectez la %s", WIFI_SSID);
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.printf("\nConectat. IP local: %s\n", WiFi.localIP().toString().c_str());
}

// ---------------------------------------------------------------- trimitere

void sendReading(float dbfs, int32_t peakToPeak) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi pierdut, reconectare...");
    wifiConnect();
    return;
  }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(2000);             // nu blocam citirea microfonului

  char body[128];
  snprintf(body, sizeof(body),
           "{\"node_id\":\"%s\",\"db\":%.1f,\"vf_vf\":%ld}",
           NODE_ID, dbfs, (long)peakToPeak);

  int code = http.POST(body);
  if (code > 0) {
    Serial.printf("%s  %.1f dB  vf-vf %ld  -> HTTP %d\n",
                  NODE_ID, dbfs, (long)peakToPeak, code);
  } else {
    Serial.printf("Trimitere esuata: %s\n", http.errorToString(code).c_str());
  }
  http.end();
}

// ---------------------------------------------------------------- setup/loop

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== SonicGrid nod %s ===\n", NODE_ID);
  Serial.printf("SCK=%d  WS=%d  SD=%d\n", I2S_SCK, I2S_WS, I2S_SD);

  i2sInit();
  Serial.println("I2S pornit.");
  wifiConnect();
}

void loop() {
  size_t bytesRead = 0;
  i2s_read(I2S_PORT, samples, sizeof(samples), &bytesRead, portMAX_DELAY);
  int n = bytesRead / sizeof(int32_t);
  if (n == 0) return;

  int32_t minV = INT32_MAX, maxV = INT32_MIN;

  for (int i = 0; i < n; i++) {
    // INMP441 da 24 de biti aliniati la stanga intr-un cuvant de 32.
    int32_t v = samples[i] >> 8;
    sumSquares += (double)v * (double)v;
    if (v < minV) minV = v;
    if (v > maxV) maxV = v;
  }
  sampleCount += n;

  int32_t p2p = maxV - minV;
  if (p2p > maxPeakToPeak) maxPeakToPeak = p2p;

  if (millis() - lastSend >= SEND_INTERVAL && sampleCount > 0) {
    double rms = sqrt(sumSquares / sampleCount);

    // 8388608 = 2^23, valoarea maxima pe 24 de biti cu semn = 0 dBFS
    float dbfs = 20.0f * log10f((float)rms / 8388608.0f + 1e-9f);
    if (dbfs < -60.0f) dbfs = -60.0f;
    if (dbfs > 0.0f)   dbfs = 0.0f;

    sendReading(dbfs, maxPeakToPeak);

    sumSquares    = 0;
    sampleCount   = 0;
    maxPeakToPeak = 0;
    lastSend      = millis();
  }
}
