#include <Arduino.h>
#include <driver/i2s.h>
#include <math.h>

const i2s_port_t I2S_PORT = I2S_NUM_0;

#define I2S_SCK 42
#define I2S_WS  40
#define I2S_SD  41

const int SAMPLE_RATE = 16000;
const int BLOCK = 256;

float GAIN = 16.0f;
// Frequencias de corte dos filtros
float HP_CUTOFF = 100.0f;
float LP_CUTOFF = 7000.0f;

// Buffers de memoria
int32_t raw[BLOCK];
char out[BLOCK * 8];

// Estado do filtro passa-alto
float hp_prev_x = 0.0f;
float hp_prev_y = 0.0f;
float hp_alpha;

// Estado do filtro passa-baixo
float lp_prev_y = 0.0f;
float lp_alpha;

String bufferCmd = "";

void setupI2S() {
    i2s_config_t cfg = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count = 8,
        .dma_buf_len = 512,
        .use_apll = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk = 0
    };

    i2s_pin_config_t pins = {
        .bck_io_num = I2S_SCK,
        .ws_io_num = I2S_WS,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num = I2S_SD
    };

    i2s_driver_install(I2S_PORT, &cfg, 0, NULL);
    i2s_set_pin(I2S_PORT, &pins);
    i2s_zero_dma_buffer(I2S_PORT);
}

float calcule_hp_alpha(float cutoff, float sample_rate){
    return expf(
        -2.0f * PI * cutoff / sample_rate
    );
}

float calcule_lp_alpha(float cutoff, float sample_rate){
    return 1.0f - expf(
        -2.0f * PI * cutoff / sample_rate
    );
}

void setup() {
    Serial.begin(2000000);
    delay(1500);

    hp_alpha = calcule_hp_alpha(HP_CUTOFF, SAMPLE_RATE);
    lp_alpha = calcule_lp_alpha(LP_CUTOFF, SAMPLE_RATE);

    setupI2S();
}

void processar_comandos(String comando){
    comando.trim();
    if (comando.startsWith("HP=")) {
        float v = comando.substring(3).toFloat();
        if (v > 0) {
            hp_alpha = calcule_hp_alpha(v, SAMPLE_RATE);
            Serial.printf("HP=%.1f\n", v);
        }
    } else if (comando.startsWith("LP=")) {
        float v = comando.substring(3).toFloat();
        if (v > 0 && v < SAMPLE_RATE / 2) {
            lp_alpha = calcule_lp_alpha(v, SAMPLE_RATE);
            Serial.printf("LP=%.1f\n", v);
        }
    } else if (comando.startsWith("GAIN=")){
        float v = comando.substring(5).toFloat();
        if(v > 0){
            GAIN = v;
            Serial.printf("GAIN=%.1f\n", v);
        }
    }
}

void capturar_audio(float gain, size_t input_bytes){
    int n = input_bytes / sizeof(int32_t);
    int len = 0;

    for (int i = 0; i < n; i++) {
        // Ajuste dos dados de 24 bits
        float x = (float)(raw[i] >> 8);

        // Filtro passa-alto
        float hp = hp_alpha *
                   (hp_prev_y + x - hp_prev_x);

        hp_prev_x = x;
        hp_prev_y = hp;

        // Filtro passa-baixo
        float lp = lp_prev_y +
                   lp_alpha * (hp - lp_prev_y);

        lp_prev_y = lp;

        // Escala e ganho
        float y = (lp / 256.0f) * GAIN;

        // Limitacao de pico para PCM de 16 bits
        if (y > 32767.0f) y = 32767.0f;
        if (y < -32768.0f) y = -32768.0f;

        len += snprintf(
            out + len,
            sizeof(out) - len,
            "%d\n",
            (int)y
        );
    }

    Serial.write((uint8_t*)out, len);
}

void loop() {

    while (Serial.available() > 0) {
        char c = Serial.read();
        if (c == '\n') {
        processar_comandos(bufferCmd);
        bufferCmd = "";
        } else if (c != '\r') {
        bufferCmd += c;
        }
    }

    size_t bytes_read = 0;

    i2s_read(
        I2S_PORT,
        raw,
        sizeof(raw),
        &bytes_read,
        portMAX_DELAY
    );

    capturar_audio(GAIN, bytes_read);

}