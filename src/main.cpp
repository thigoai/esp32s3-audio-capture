#include <Arduino.h>
#include <driver/i2s.h>

const i2s_port_t I2S_PORT = I2S_NUM_0;
#define I2S_SCK 42
#define I2S_WS  40
#define I2S_SD  41

/*
taxa de amostragem, 
que é a quantidade de vezes 
por segundo que o mic captura um som.
*/
const int SAMPLE_RATE = 16000; 

const int BLOCK = 256; // quantidade de amostras processadas por vez
const float GAIN = 16.0f;   // multiplicador de volume (ganho)

// buffers de mémoria 
int32_t raw[BLOCK];
char out[BLOCK * 8];
float dc = 0;

// variaveis do filtro passa-alto
float hp_prev_x = 0, hp_prev_y = 0;
const float HP = 0.96f;  // coeficiente de passa-alto

void setupI2S() {
    i2s_config_t cfg = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate = SAMPLE_RATE,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_I2S,
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

void setup() {
    Serial.begin(2000000);  
    setupI2S();
}


void loop() {

    size_t bytes_read = 0;
  
    // Lê os dados I2S bloqueando a execução até que o buffer 'raw' esteja preenchido
    i2s_read(I2S_PORT, raw, sizeof(raw), &bytes_read, portMAX_DELAY);


    int n = bytes_read / sizeof(int32_t); // calcula o num. de amostras recebidas
    int len = 0;
  
    // Processa cada amostra de áudio recebida no bloco
    for (int i = 0; i < n; i++) {

        /*
        Desloca 8 bits para a direita para ajustar o alinhamento dos 
        dados de 24 bits dentro do inteiro de 32 bits
        */
        float x = (float)(raw[i] >> 8);
        
        // Aplica o filtro passa-alto para remover o DC offset
        float h = HP * (hp_prev_y + x - hp_prev_x);
        hp_prev_x = x;
        hp_prev_y = h;

        // Divide por 256 para escalar a amplitude e multiplica pelo Ganho desejado
        float y = h / 256.0f * GAIN;

        // Limitação de pico para evitar audio estourado
        // limites de um int de 16 bits (-32768 a 32767)
        if (y > 32767) y = 32767;
        if (y < -32768) y = -32768;
        len += snprintf(out + len, sizeof(out) - len, "%d\n", (int)y);
    }

    // Envia todo o bloco processado de uma vez só via Seria
    Serial.write((uint8_t*)out, len);      
}