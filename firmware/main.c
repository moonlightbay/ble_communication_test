/*
 * EEG streaming firmware example
 * - Provides custom BLE UART-style service with read/write/notify characteristics
 * - Streams 16-channel synthetic EEG frames at 500 Hz after receiving 0xAA
 * - Packages five sequential frames per notification
 * - Each frame = timestamp (µs) + sequence + 16 × int16 samples
 * - Host can send 0xFF to stop and query the last sequence via read characteristic
 */

#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

#include "nrf_sdh.h"
#include "nrf_sdh_ble.h"
#include "nrf_sdh_soc.h"
#include "nrf_ble_gatt.h"
#include "nrf_ble_qwr.h"
#include "nrf_pwr_mgmt.h"
#include "app_timer.h"
#include "ble_advdata.h"
#include "ble_advertising.h"
#include "ble_conn_params.h"
#include "ble_srv_common.h"
#include "nrf_log.h"
#include "nrf_log_ctrl.h"
#include "nrf_log_default_backends.h"
#include "boards.h"
#include "app_uart.h"
#if defined (UART_PRESENT)
#include "nrf_uart.h"
#endif
#if defined (UARTE_PRESENT)
#include "nrf_uarte.h"
#endif

#include "sdk_config.h"
#include "nrfx_timer.h"
#include "my_ble_uarts.h"

#define DEVICE_NAME                     "bio-eeg"                          /* EEG设备在GAP广播中显示的名称 */
#define CUSTOM_MAC_LEAST_FIRST          {0x01, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE} /* 自定义MAC地址（低字节在前） */
#define UARTS_SERVICE_UUID_TYPE         BLE_UUID_TYPE_VENDOR_BEGIN
#define APP_BLE_OBSERVER_PRIO           3
#define APP_BLE_CONN_CFG_TAG            1

#define APP_ADV_INTERVAL                64
#define APP_ADV_DURATION                0

#define MIN_CONN_INTERVAL               (6)  /* 7.5 ms (6 × 1.25 ms) */
#define MAX_CONN_INTERVAL               (8)  /* 10 ms (8 × 1.25 ms) */
#define SLAVE_LATENCY                   0
#define CONN_SUP_TIMEOUT                MSEC_TO_UNITS(4000, UNIT_10_MS)

#define FIRST_CONN_PARAMS_UPDATE_DELAY  APP_TIMER_TICKS(500)
#define NEXT_CONN_PARAMS_UPDATE_DELAY   APP_TIMER_TICKS(5000)
#define MAX_CONN_PARAMS_UPDATE_COUNT    3

/* Timing and payload layout */
#define STREAM_INTERVAL_TICKS           APP_TIMER_TICKS(2) /* 500Hz (2ms) */
/*
 * Select channel mode at build time:
 * - Define STREAM_CHANNELS as 16 or 32 (default 16)
 */
#ifndef STREAM_CHANNELS
#define STREAM_CHANNELS                 32
#endif
#define CHANNEL_COUNT                   STREAM_CHANNELS
#define BATCH_FRAME_COUNT               5
#define BYTES_PER_SAMPLE                (CHANNEL_COUNT * sizeof(int16_t))
#define FRAME_HEADER_SIZE               (sizeof(uint32_t) + sizeof(uint32_t)) /* timestamp + sequence */
#define FRAME_TOTAL_SIZE                (FRAME_HEADER_SIZE + BYTES_PER_SAMPLE)
#define BATCH_TOTAL_SIZE                (BATCH_FRAME_COUNT * FRAME_TOTAL_SIZE)
/* Conservative application payload limit per notification (ATT MTU 247 -> ~244B).
 * We dynamically reduce frames per notification when CHANNEL_COUNT increases. */
#define MAX_NOTIFY_PAYLOAD              244u
#define START_COMMAND                   0xAA /* host command: start streaming */
#define STOP_COMMAND                    0xFF /* host command: stop streaming */
#define STATUS_CHAR_UUID                0x000D
#define UART_TX_BUF_SIZE                256
#define UART_RX_BUF_SIZE                256

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* Module singletons */
BLE_UARTS_DEF(m_uarts, NRF_SDH_BLE_TOTAL_LINK_COUNT);
NRF_BLE_GATT_DEF(m_gatt);
NRF_BLE_QWR_DEF(m_qwr);
BLE_ADVERTISING_DEF(m_advertising);
APP_TIMER_DEF(m_stream_timer_id);

/* Runtime state */
static uint16_t m_conn_handle = BLE_CONN_HANDLE_INVALID;
static bool     m_streaming_enabled = false;
static uint32_t m_sample_tick = 0;              /* synthesised signal phase accumulator */
static uint32_t m_frame_sequence = 0;           /* running frame counter while streaming */
static uint32_t m_last_stream_sequence = 0;     /* published when streaming stops */
static uint32_t m_stream_start_timestamp = 0;   /* capture of timer when streaming begins */
static uint8_t  m_stream_payload[BATCH_TOTAL_SIZE]; /* notify buffer: batched frames */
static ble_gatts_char_handles_t m_status_handles;
static nrfx_timer_t m_timestamp_timer = NRFX_TIMER_INSTANCE(4); /* 32-bit, 1 MHz wall-clock */
static bool     m_tx_pending = false;           /* true while a frame awaits SoftDevice credits */
static uint16_t m_pending_length = 0;           /* cached length for the pending frame */
static uint32_t m_pending_sequence = 0;         /* sequence number associated with pending frame */
static uint32_t m_frame_drops = 0;              /* frames discarded due to buffer overflow */

#define FRAME_BUFFER_MULTIPLIER         100u
#define FRAME_BUFFER_CAPACITY           (BATCH_FRAME_COUNT * FRAME_BUFFER_MULTIPLIER) /* room for 500 frames默认 */

typedef struct
{
    uint8_t data[FRAME_TOTAL_SIZE];
} frame_buffer_entry_t;

static frame_buffer_entry_t m_frame_buffer[FRAME_BUFFER_CAPACITY];
static uint16_t m_frame_buffer_head = 0;        /* index of the oldest pending frame */
static uint16_t m_frame_buffer_count = 0;       /* number of frames queued for transmission */
static uint16_t m_frame_buffer_high_watermark = 0; /* peak frames queued (telemetry) */

/* Advertising payload (UARTS service UUID) */
static ble_uuid_t m_adv_uuids[] = {
    { BLE_UUID_UARTS_SERVICE, UARTS_SERVICE_UUID_TYPE }
};

/* Forward declarations */
static void stream_timer_handler(void * p_context);
static void advertising_start(void);
static void uart_init(void);
static void status_char_init(void);
static void status_char_update(uint32_t frame_total);
static void timestamp_timer_init(void);
static uint32_t timestamp_now_us(void);
static void try_send_pending_frame(void);
static void prepare_next_batch(void);
static void flush_pending_batch(void);

static uint32_t read_le32(uint8_t const * p_src)
{
    return ((uint32_t)p_src[0]) |
           ((uint32_t)p_src[1] << 8) |
           ((uint32_t)p_src[2] << 16) |
           ((uint32_t)p_src[3] << 24);
}

static ble_gap_conn_params_t m_connection_params = {
    .min_conn_interval = MIN_CONN_INTERVAL,
    .max_conn_interval = MAX_CONN_INTERVAL,
    .slave_latency     = SLAVE_LATENCY,
    .conn_sup_timeout  = CONN_SUP_TIMEOUT,
};

/* Minimal UART error handler required by app_uart FIFO backend */
static void uart_event_handle(app_uart_evt_t * p_event)
{
    if (p_event == NULL)
    {
        return;
    }

    switch (p_event->evt_type)
    {
        case APP_UART_COMMUNICATION_ERROR:
            APP_ERROR_HANDLER(p_event->data.error_communication);
            break;

        case APP_UART_FIFO_ERROR:
            APP_ERROR_HANDLER(p_event->data.error_code);
            break;

        default:
            break;
    }
}

/* Configure app_uart so printf() can report progress over the hardware UART */
static void uart_init(void)
{
    const app_uart_comm_params_t comm_params = {
        RX_PIN_NUMBER,
        TX_PIN_NUMBER,
        RTS_PIN_NUMBER,
        CTS_PIN_NUMBER,
        APP_UART_FLOW_CONTROL_DISABLED,
        false,
        NRF_UART_BAUDRATE_115200
    };

    uint32_t err_code;
    APP_UART_FIFO_INIT(&comm_params,
                       UART_RX_BUF_SIZE,
                       UART_TX_BUF_SIZE,
                       uart_event_handle,
                       APP_IRQ_PRIORITY_LOWEST,
                       err_code);
    if ((err_code != NRF_SUCCESS) && (err_code != NRF_ERROR_INVALID_STATE))
    {
        APP_ERROR_CHECK(err_code);
    }

    printf("UART ready\r\n");
}

/* Build a deterministic synthetic EEG sample for a given channel/tick */
static int16_t synth_signal(uint8_t channel, uint32_t tick)
{
    const float phase = ((float)tick * 0.01f) + ((float)channel * 0.05f);
    const float wave  = 0.8f * sinf(2.0f * (float)M_PI * phase);
    const float noise = 0.2f * sinf(0.25f * (float)M_PI * phase);

    int32_t value = (int32_t)((wave + noise) * 32767.0f);
    if (value > INT16_MAX)
    {
        value = INT16_MAX;
    }
    else if (value < INT16_MIN)
    {
        value = INT16_MIN;
    }
    return (int16_t)value;
}

/* Periodic timer callback that assembles and transmits one EEG frame */
static void stream_timer_handler(void * p_context)
{
    (void)p_context;

    if (!m_streaming_enabled || (m_conn_handle == BLE_CONN_HANDLE_INVALID))
    {
        return;
    }

    uint16_t insert_index = (m_frame_buffer_head + m_frame_buffer_count) % FRAME_BUFFER_CAPACITY;
    if (m_frame_buffer_count == FRAME_BUFFER_CAPACITY)
    {
        /* Buffer full: drop the oldest frame to make room. */
        m_frame_buffer_head = (m_frame_buffer_head + 1) % FRAME_BUFFER_CAPACITY;
        m_frame_buffer_count--;
        m_frame_drops++;
        NRF_LOG_WARNING("Frame buffer overflow (drops=%lu)", (unsigned long)m_frame_drops);
    }

    frame_buffer_entry_t * slot = &m_frame_buffer[insert_index];

    uint32_t frame_seq    = ++m_frame_sequence;
    uint32_t timestamp_us = timestamp_now_us() - m_stream_start_timestamp;

    slot->data[0] = (uint8_t)(timestamp_us & 0xFF);
    slot->data[1] = (uint8_t)((timestamp_us >> 8) & 0xFF);
    slot->data[2] = (uint8_t)((timestamp_us >> 16) & 0xFF);
    slot->data[3] = (uint8_t)((timestamp_us >> 24) & 0xFF);
    slot->data[4] = (uint8_t)(frame_seq & 0xFF);
    slot->data[5] = (uint8_t)((frame_seq >> 8) & 0xFF);
    slot->data[6] = (uint8_t)((frame_seq >> 16) & 0xFF);
    slot->data[7] = (uint8_t)((frame_seq >> 24) & 0xFF);

    uint16_t index = FRAME_HEADER_SIZE;
    for (uint8_t ch = 0; ch < CHANNEL_COUNT; ch++)
    {
        int16_t sample = synth_signal(ch, m_sample_tick);
        slot->data[index++] = (uint8_t)(sample & 0xFF);
        slot->data[index++] = (uint8_t)((sample >> 8) & 0xFF);
    }
    m_sample_tick++;

    m_frame_buffer_count++;
    if (m_frame_buffer_count > m_frame_buffer_high_watermark)
    {
        m_frame_buffer_high_watermark = m_frame_buffer_count;
    }
    try_send_pending_frame();
}

/* BLE connection-parameter negotiation callback */
static void on_conn_params_evt(ble_conn_params_evt_t * p_evt)
{
    if (p_evt->evt_type == BLE_CONN_PARAMS_EVT_FAILED)
    {
        ret_code_t err_code = sd_ble_gap_disconnect(m_conn_handle, BLE_HCI_CONN_INTERVAL_UNACCEPTABLE);
        APP_ERROR_CHECK(err_code);
    }
}

/* Fatal error helper used by the connection-parameter module */
static void conn_params_error_handler(uint32_t nrf_error)
{
    APP_ERROR_HANDLER(nrf_error);
}

/* Configure GAP identity, preferred connection params, and custom MAC */
static void gap_params_init(void)
{
    ble_gap_conn_sec_mode_t sec_mode;
    BLE_GAP_CONN_SEC_MODE_SET_OPEN(&sec_mode);

    ble_gap_addr_t custom_addr = {
        .addr_type = BLE_GAP_ADDR_TYPE_RANDOM_STATIC,
        .addr      = CUSTOM_MAC_LEAST_FIRST
    };

    ret_code_t err_code = sd_ble_gap_addr_set(&custom_addr);
    APP_ERROR_CHECK(err_code);

    err_code = sd_ble_gap_device_name_set(&sec_mode,
                                          (const uint8_t *)DEVICE_NAME,
                                          strlen(DEVICE_NAME));
    APP_ERROR_CHECK(err_code);

    ble_gap_conn_params_t gap_conn_params = m_connection_params;

    err_code = sd_ble_gap_ppcp_set(&gap_conn_params);
    APP_ERROR_CHECK(err_code);

    printf("GAP ready, custom MAC applied\r\n");
}

/* Initialise the SoftDevice GATT module */
static void gatt_init(void)
{
    ret_code_t err_code = nrf_ble_gatt_init(&m_gatt, NULL);
    APP_ERROR_CHECK(err_code);

    err_code = nrf_ble_gatt_att_mtu_periph_set(&m_gatt, NRF_SDH_BLE_GATT_MAX_MTU_SIZE);
    APP_ERROR_CHECK(err_code);
}

/* Required error callback for the Queued Write module */
static void nrf_qwr_error_handler(uint32_t nrf_error)
{
    APP_ERROR_HANDLER(nrf_error);
}

/* Prepare the Queued Write module (needed by some SDK services) */
static void qwr_init(void)
{
    nrf_ble_qwr_init_t qwr_init = {0};
    qwr_init.error_handler = nrf_qwr_error_handler;

    ret_code_t err_code = nrf_ble_qwr_init(&m_qwr, &qwr_init);
    APP_ERROR_CHECK(err_code);
}

/* Configure TIMER4 as a free-running 1 MHz counter for timestamps */
static void timestamp_timer_init(void)
{
    nrfx_timer_config_t const cfg = {
        .frequency          = NRF_TIMER_FREQ_1MHz,
        .mode               = NRF_TIMER_MODE_TIMER,
        .bit_width          = NRF_TIMER_BIT_WIDTH_32,
        .interrupt_priority = APP_IRQ_PRIORITY_LOWEST,
        .p_context          = NULL
    };

    nrfx_err_t err = nrfx_timer_init(&m_timestamp_timer, &cfg, NULL);
    if ((err != NRFX_SUCCESS) && (err != NRFX_ERROR_INVALID_STATE))
    {
        APP_ERROR_CHECK(err);
    }

    nrfx_timer_clear(&m_timestamp_timer);
    nrfx_timer_enable(&m_timestamp_timer);
}

/* Read the current 1 MHz counter value (wraps every ~4295 seconds) */
static uint32_t timestamp_now_us(void)
{
    return nrfx_timer_capture(&m_timestamp_timer, NRF_TIMER_CC_CHANNEL0);
}

/* Attempt to push the cached payload once the SoftDevice has TX credits. */
static void try_send_pending_frame(void)
{
    if (m_conn_handle == BLE_CONN_HANDLE_INVALID)
    {
        return;
    }

    if (!m_tx_pending)
    {
        prepare_next_batch();
    }

    while (m_tx_pending)
    {
        uint16_t length = m_pending_length;
        ret_code_t err_code = ble_uarts_data_send(&m_uarts, m_stream_payload, &length, m_conn_handle);
        if (err_code == NRF_SUCCESS)
        {
            m_last_stream_sequence = m_pending_sequence;
            m_tx_pending = false;
            m_pending_length = 0;
            m_pending_sequence = 0;
            status_char_update(m_last_stream_sequence);
            prepare_next_batch();
        }
        else if (err_code == NRF_ERROR_RESOURCES)
        {
            /* SoftDevice buffers full: wait for BLE_UARTS_EVT_TX_RDY. */
            return;
        }
        else
        {
            NRF_LOG_WARNING("Data send failed: 0x%X", err_code);
            m_tx_pending = false;
            m_pending_length = 0;
            m_pending_sequence = 0;
            prepare_next_batch();
        }
    }
}

static void prepare_next_batch(void)
{
    if (m_tx_pending || (m_frame_buffer_count == 0))
    {
        return;
    }

    /* Limit frames per notification by both backlog and payload budget. */
    uint8_t backlog_limit = (m_frame_buffer_count > BATCH_FRAME_COUNT) ? BATCH_FRAME_COUNT : (uint8_t)m_frame_buffer_count;
    uint8_t payload_limit = (uint8_t)(MAX_NOTIFY_PAYLOAD / FRAME_TOTAL_SIZE);
    if (payload_limit == 0) { payload_limit = 1; }
    uint8_t frame_count = (backlog_limit < payload_limit) ? backlog_limit : payload_limit;
    uint16_t payload_offset = 0;

    for (uint8_t i = 0; i < frame_count; ++i)
    {
        uint16_t buffer_index = (uint16_t)((m_frame_buffer_head + i) % FRAME_BUFFER_CAPACITY);
        memcpy(&m_stream_payload[payload_offset], m_frame_buffer[buffer_index].data, FRAME_TOTAL_SIZE);
        payload_offset = (uint16_t)(payload_offset + FRAME_TOTAL_SIZE);
    }

    uint8_t const * last_frame = &m_stream_payload[(frame_count - 1) * FRAME_TOTAL_SIZE];
    m_pending_sequence = read_le32(&last_frame[4]);
    m_pending_length = (uint16_t)(frame_count * FRAME_TOTAL_SIZE);
    m_tx_pending = true;

    m_frame_buffer_head = (uint16_t)((m_frame_buffer_head + frame_count) % FRAME_BUFFER_CAPACITY);
    m_frame_buffer_count = (uint16_t)(m_frame_buffer_count - frame_count);
}

static void flush_pending_batch(void)
{
    try_send_pending_frame();
}

/* Add a readable characteristic that mirrors the last completed frame ID */
static void status_char_init(void)
{
    ble_add_char_params_t params;
    memset(&params, 0, sizeof(params));

    params.uuid              = STATUS_CHAR_UUID;
    params.uuid_type         = m_uarts.uuid_type;
    params.init_len          = sizeof(uint32_t);
    params.max_len           = sizeof(uint32_t);
    params.is_var_len        = false;
    params.char_props.read   = 1;
    params.read_access       = SEC_OPEN;

    /* Expose the last completed frame sequence via a readable characteristic. */

    uint32_t initial_status = 0;
    params.p_init_value = (uint8_t *)&initial_status;

    ret_code_t err_code = characteristic_add(m_uarts.service_handle, &params, &m_status_handles);
    APP_ERROR_CHECK(err_code);

    status_char_update(0);
    printf("Status characteristic ready\r\n");
}

/* Persist the provided frame count into the read characteristic */
static void status_char_update(uint32_t frame_total)
{
    ble_gatts_value_t value;
    memset(&value, 0, sizeof(value));
    value.len     = sizeof(frame_total);
    value.p_value = (uint8_t *)&frame_total;

    ret_code_t err_code = sd_ble_gatts_value_set(BLE_CONN_HANDLE_INVALID,
                                                 m_status_handles.value_handle,
                                                 &value);
    APP_ERROR_CHECK(err_code);
}

/* Event callback from the custom UARTS service (commands + notification state) */
static void uarts_data_handler(ble_uarts_evt_t * p_evt)
{
    if (p_evt == NULL)
    {
        return;
    }

    switch (p_evt->type)
    {
        case BLE_NUS_EVT_COMM_STARTED:
            printf("Notifications enabled\r\n");
            break;

        case BLE_NUS_EVT_COMM_STOPPED:
            printf("Notifications disabled (frames=%lu)\r\n", (unsigned long)m_last_stream_sequence);
            m_streaming_enabled = false;
            app_timer_stop(m_stream_timer_id);
            m_tx_pending = false;
            m_pending_length = 0;
            m_pending_sequence = 0;
            m_frame_buffer_head = 0;
            m_frame_buffer_count = 0;
            printf("Buffer stats: drops=%lu high_water=%u\r\n",
                   (unsigned long)m_frame_drops,
                   (unsigned)m_frame_buffer_high_watermark);
            status_char_update(m_last_stream_sequence);
            break;

        case BLE_UARTS_EVT_RX_DATA:
            if ((p_evt->params.rx_data.length > 0) &&
                (p_evt->params.rx_data.p_data[0] == START_COMMAND))
            {
                /* 0xAA from host arms the streaming timer. */
                if (!m_streaming_enabled)
                {
                    m_streaming_enabled = true;
                    m_sample_tick = 0;
                    m_frame_sequence = 0;
                    m_last_stream_sequence = 0;
                    m_stream_start_timestamp = timestamp_now_us();
                    m_tx_pending = false;
                    m_pending_length = 0;
                    m_pending_sequence = 0;
                    m_frame_buffer_head = 0;
                    m_frame_buffer_count = 0;
                    m_frame_buffer_high_watermark = 0;
                    m_frame_drops = 0;
                    status_char_update(0);
                    ret_code_t err_code = app_timer_start(m_stream_timer_id, STREAM_INTERVAL_TICKS, NULL);
                    APP_ERROR_CHECK(err_code);
                    printf("Streaming started\r\n");
                }
            }
            else if ((p_evt->params.rx_data.length > 0) &&
                     (p_evt->params.rx_data.p_data[0] == STOP_COMMAND))
            {
                /* 0xFF from host stops streaming and publishes the total frame count. */
                if (m_streaming_enabled)
                {
                    flush_pending_batch();
                    m_streaming_enabled = false;
                    app_timer_stop(m_stream_timer_id);
                    status_char_update(m_last_stream_sequence);
                    printf("Buffer stats: drops=%lu high_water=%u\r\n",
                           (unsigned long)m_frame_drops,
                           (unsigned)m_frame_buffer_high_watermark);
                    printf("Streaming stopped by host (frames=%lu)\r\n", (unsigned long)m_last_stream_sequence);
                }
            }
            break;

        case BLE_UARTS_EVT_TX_RDY:
            try_send_pending_frame();
            break;

        default:
            break;
    }
}

/* Instantiate the custom UARTS service */
static void uarts_init(void)
{
    ble_uarts_init_t uarts_init_cfg = {0};
    uarts_init_cfg.data_handler = uarts_data_handler;

    ret_code_t err_code = ble_uarts_init(&m_uarts, &uarts_init_cfg);
    APP_ERROR_CHECK(err_code);

    printf("UART service ready\r\n");
}

/* Bundle all service initialisation steps */
static void services_init(void)
{
    qwr_init();
    uarts_init();
    status_char_init();
}

/* Configure automatic connection-parameter negotiation */
static void conn_params_init(void)
{
    ble_conn_params_init_t cp_init;
    memset(&cp_init, 0, sizeof(cp_init));

    cp_init.p_conn_params                  = &m_connection_params;
    cp_init.first_conn_params_update_delay = FIRST_CONN_PARAMS_UPDATE_DELAY;
    cp_init.next_conn_params_update_delay  = NEXT_CONN_PARAMS_UPDATE_DELAY;
    cp_init.max_conn_params_update_count   = MAX_CONN_PARAMS_UPDATE_COUNT;
    cp_init.start_on_notify_cccd_handle    = BLE_GATT_HANDLE_INVALID;
    cp_init.disconnect_on_fail             = false;
    cp_init.evt_handler                    = on_conn_params_evt;
    cp_init.error_handler                  = conn_params_error_handler;

    ret_code_t err_code = ble_conn_params_init(&cp_init);
    APP_ERROR_CHECK(err_code);
}

/* Build the advertising payload and parameters */
static void advertising_init(void)
{
    ble_advertising_init_t init;
    memset(&init, 0, sizeof(init));

    init.advdata.name_type               = BLE_ADVDATA_FULL_NAME;
    init.advdata.include_appearance      = false;
    init.advdata.flags                   = BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE;
    init.advdata.uuids_complete.uuid_cnt = sizeof(m_adv_uuids) / sizeof(m_adv_uuids[0]);
    init.advdata.uuids_complete.p_uuids  = m_adv_uuids;

    init.config.ble_adv_fast_enabled  = true;
    init.config.ble_adv_fast_interval = APP_ADV_INTERVAL;
    init.config.ble_adv_fast_timeout  = APP_ADV_DURATION;
    init.evt_handler                  = NULL;

    ret_code_t err_code = ble_advertising_init(&m_advertising, &init);
    APP_ERROR_CHECK(err_code);

    ble_advertising_conn_cfg_tag_set(&m_advertising, APP_BLE_CONN_CFG_TAG);
}

/* Handle high-level BLE GAP events (connect/disconnect) */
static void ble_evt_handler(ble_evt_t const * p_ble_evt, void * p_context)
{
    if (p_ble_evt == NULL)
    {
        return;
    }

    switch (p_ble_evt->header.evt_id)
    {
        case BLE_GAP_EVT_CONNECTED:
            printf("Connected\r\n");
            m_conn_handle = p_ble_evt->evt.gap_evt.conn_handle;
            m_tx_pending = false;
            m_pending_length = 0;
            m_pending_sequence = 0;
            m_frame_buffer_head = 0;
            m_frame_buffer_count = 0;
            m_frame_buffer_high_watermark = 0;
            m_frame_drops = 0;
            status_char_update(m_last_stream_sequence);

            {
                ble_gap_data_length_params_t const dlp = {
                    .max_rx_octets = BLE_GAP_DATA_LENGTH_AUTO,
                    .max_tx_octets = BLE_GAP_DATA_LENGTH_AUTO,
                    .max_rx_time_us = BLE_GAP_DATA_LENGTH_AUTO,
                    .max_tx_time_us = BLE_GAP_DATA_LENGTH_AUTO,
                };

                uint32_t err_code = sd_ble_gap_data_length_update(m_conn_handle, &dlp, NULL);
                if ((err_code != NRF_SUCCESS) &&
                    (err_code != NRF_ERROR_BUSY) &&
                    (err_code != NRF_ERROR_INVALID_STATE) &&
                    (err_code != NRF_ERROR_NOT_SUPPORTED))
                {
                    APP_ERROR_CHECK(err_code);
                }

                ble_gap_phys_t const phys = {
                    .rx_phys = BLE_GAP_PHY_2MBPS,
                    .tx_phys = BLE_GAP_PHY_2MBPS,
                };

                err_code = sd_ble_gap_phy_update(m_conn_handle, &phys);
                if ((err_code != NRF_SUCCESS) &&
                    (err_code != NRF_ERROR_BUSY) &&
                    (err_code != NRF_ERROR_INVALID_STATE) &&
                    (err_code != NRF_ERROR_NOT_SUPPORTED))
                {
                    APP_ERROR_CHECK(err_code);
                }
            }
            break;

        case BLE_GAP_EVT_DISCONNECTED:
            printf("Disconnected\r\n");
            m_conn_handle = BLE_CONN_HANDLE_INVALID;
            m_streaming_enabled = false;
            m_tx_pending = false;
            m_pending_length = 0;
            m_pending_sequence = 0;
            m_frame_buffer_head = 0;
            m_frame_buffer_count = 0;
            m_frame_buffer_high_watermark = 0;
            status_char_update(m_last_stream_sequence);
            app_timer_stop(m_stream_timer_id);
            advertising_start();
            break;

        default:
            break;
    }
}

NRF_SDH_BLE_OBSERVER(m_ble_observer, APP_BLE_OBSERVER_PRIO, ble_evt_handler, NULL);

/* Start fast advertising and report via UART */
static void advertising_start(void)
{
    ret_code_t err_code = ble_advertising_start(&m_advertising, BLE_ADV_MODE_FAST);
    APP_ERROR_CHECK(err_code);
    printf("Advertising\r\n");
}

/* Set up application timers used by this firmware */
static void timers_init(void)
{
    ret_code_t err_code = app_timer_init();
    APP_ERROR_CHECK(err_code);

    err_code = app_timer_create(&m_stream_timer_id,
                                APP_TIMER_MODE_REPEATED,
                                stream_timer_handler);
    APP_ERROR_CHECK(err_code);
}

/* Configure the SDK logging backend */
static void log_init(void)
{
    ret_code_t err_code = NRF_LOG_INIT(NULL);
    APP_ERROR_CHECK(err_code);

    NRF_LOG_DEFAULT_BACKENDS_INIT();
}

/* Prepare the power-management helper (needed when idle) */
static void power_management_init(void)
{
    ret_code_t err_code = nrf_pwr_mgmt_init();
    APP_ERROR_CHECK(err_code);
}

/* Enable the SoftDevice and apply default BLE configuration */
static void ble_stack_init(void)
{
    ret_code_t err_code = nrf_sdh_enable_request();
    APP_ERROR_CHECK(err_code);

    uint32_t ram_start = 0;
    err_code = nrf_sdh_ble_default_cfg_set(APP_BLE_CONN_CFG_TAG, &ram_start);
    APP_ERROR_CHECK(err_code);

    err_code = nrf_sdh_ble_enable(&ram_start);
    APP_ERROR_CHECK(err_code);
}

/* Sleep until the next event while flushing pending logs */
static void idle_state_handle(void)
{
    if (NRF_LOG_PROCESS() == false)
    {
        nrf_pwr_mgmt_run();
    }
}

/* Application entry point: initialise subsystems then enter the idle loop */
int main(void)
{
    uart_init();
    log_init();
    timers_init();
    timestamp_timer_init();
    power_management_init();
    ble_stack_init();
    gap_params_init();
    gatt_init();
    services_init();
    advertising_init();
    conn_params_init();

    advertising_start();

    printf("EEG firmware ready\r\n");

    for (;;)
    {
        idle_state_handle();
    }
}
