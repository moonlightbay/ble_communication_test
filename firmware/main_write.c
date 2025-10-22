/*
 * Stimulator/Exoskeleton write-test firmware
 * - Implements the same custom UART-style BLE service as main.c (my_ble_uarts)
 * - No notification streaming; only accepts 8-byte write payloads from the host
 * - Host sends 64-bit little-endian timestamps (µs) relative to its first write
 * - Firmware prints host/local timing delta and exposes a 16-bit packet count over the read characteristic
 */

#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>

#include "nrf_sdh.h"
#include "nrf_sdh_ble.h"
#include "nrf_sdh_soc.h"
#include "nrf_ble_gatt.h"
#include "nrf_ble_qwr.h"
#include "nrf_pwr_mgmt.h"
#include "ble_advdata.h"
#include "ble_advertising.h"
#include "ble_conn_params.h"
#include "ble_srv_common.h"
#include "app_timer.h"
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

#define DEVICE_NAME                     "bio-stim"                         /* Default GAP device name; adjust before flashing */
#define CUSTOM_MAC_LEAST_FIRST          {0x04, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE} /* Random static address (LSB first) */
#define UARTS_SERVICE_UUID_TYPE         BLE_UUID_TYPE_VENDOR_BEGIN
#define APP_BLE_OBSERVER_PRIO           3
#define APP_BLE_CONN_CFG_TAG            1

#define APP_ADV_INTERVAL                64
#define APP_ADV_DURATION                0

#define MIN_CONN_INTERVAL               (6)  /* 7.5 ms (6 × 1.25 ms) */
#define MAX_CONN_INTERVAL               (50) /* 62.5 ms upper bound for writers */
#define SLAVE_LATENCY                   0
#define CONN_SUP_TIMEOUT                MSEC_TO_UNITS(4000, UNIT_10_MS)

#define FIRST_CONN_PARAMS_UPDATE_DELAY  APP_TIMER_TICKS(500)
#define NEXT_CONN_PARAMS_UPDATE_DELAY   APP_TIMER_TICKS(5000)
#define MAX_CONN_PARAMS_UPDATE_COUNT    3

#define STATUS_CHAR_UUID                0x000D
#define WRITE_PAYLOAD_SIZE              8u

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

BLE_UARTS_DEF(m_uarts, NRF_SDH_BLE_TOTAL_LINK_COUNT);
NRF_BLE_GATT_DEF(m_gatt);
NRF_BLE_QWR_DEF(m_qwr);
BLE_ADVERTISING_DEF(m_advertising);

static uint16_t m_conn_handle = BLE_CONN_HANDLE_INVALID;
static ble_gatts_char_handles_t m_status_handles;
static nrfx_timer_t m_timestamp_timer = NRFX_TIMER_INSTANCE(4); /* 32-bit, 1 MHz */

static bool     m_has_write_base = false;
static uint32_t m_local_base_us = 0;
static uint64_t m_host_base_us = 0;
static uint32_t m_write_count = 0;

static ble_gap_conn_params_t m_connection_params = {
    .min_conn_interval = MIN_CONN_INTERVAL,
    .max_conn_interval = MAX_CONN_INTERVAL,
    .slave_latency     = SLAVE_LATENCY,
    .conn_sup_timeout  = CONN_SUP_TIMEOUT,
};

static uint16_t m_last_status_value = 0;

static void advertising_start(void);
static void timestamp_timer_init(void);
static uint32_t timestamp_now_us(void);
static void status_char_update(uint16_t packet_count);
static void on_conn_params_evt(ble_conn_params_evt_t * p_evt);
static void uarts_data_handler(ble_uarts_evt_t * p_evt);

static uint32_t read_le32(uint8_t const * p_src)
{
    return ((uint32_t)p_src[0]) |
           ((uint32_t)p_src[1] << 8) |
           ((uint32_t)p_src[2] << 16) |
           ((uint32_t)p_src[3] << 24);
}

static uint64_t read_le64(uint8_t const * p_src)
{
    uint32_t lo = read_le32(p_src);
    uint32_t hi = read_le32(p_src + 4);
    return ((uint64_t)hi << 32) | lo;
}

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
                       256,
                       256,
                       uart_event_handle,
                       APP_IRQ_PRIORITY_LOWEST,
                       err_code);
    if ((err_code != NRF_SUCCESS) && (err_code != NRF_ERROR_INVALID_STATE))
    {
        APP_ERROR_CHECK(err_code);
    }

    printf("UART ready\r\n");
}

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

static void gatt_init(void)
{
    ret_code_t err_code = nrf_ble_gatt_init(&m_gatt, NULL);
    APP_ERROR_CHECK(err_code);

    err_code = nrf_ble_gatt_att_mtu_periph_set(&m_gatt, NRF_SDH_BLE_GATT_MAX_MTU_SIZE);
    APP_ERROR_CHECK(err_code);
}

static void nrf_qwr_error_handler(uint32_t nrf_error)
{
    APP_ERROR_HANDLER(nrf_error);
}

static void qwr_init(void)
{
    nrf_ble_qwr_init_t qwr_init = {0};
    qwr_init.error_handler = nrf_qwr_error_handler;

    ret_code_t err_code = nrf_ble_qwr_init(&m_qwr, &qwr_init);
    APP_ERROR_CHECK(err_code);
}

static void status_char_init(void)
{
    ble_add_char_params_t params;
    memset(&params, 0, sizeof(params));

    params.uuid              = STATUS_CHAR_UUID;
    params.uuid_type         = m_uarts.uuid_type;
    params.init_len          = sizeof(uint16_t);
    params.max_len           = sizeof(uint16_t);
    params.is_var_len        = false;
    params.char_props.read   = 1;
    params.read_access       = SEC_OPEN;

    uint16_t initial_value = 0;
    params.p_init_value = (uint8_t *)&initial_value;

    ret_code_t err_code = characteristic_add(m_uarts.service_handle, &params, &m_status_handles);
    APP_ERROR_CHECK(err_code);

    status_char_update(0);
    printf("Status characteristic ready\r\n");
}

static void status_char_update(uint16_t packet_count)
{
    if (m_last_status_value == packet_count)
    {
        return;
    }

    ble_gatts_value_t value;
    memset(&value, 0, sizeof(value));
    value.len     = sizeof(packet_count);
    value.p_value = (uint8_t *)&packet_count;

    ret_code_t err_code = sd_ble_gatts_value_set(BLE_CONN_HANDLE_INVALID,
                                                 m_status_handles.value_handle,
                                                 &value);
    APP_ERROR_CHECK(err_code);
    m_last_status_value = packet_count;
}

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

static uint32_t timestamp_now_us(void)
{
    return nrfx_timer_capture(&m_timestamp_timer, NRF_TIMER_CC_CHANNEL0);
}

static void services_init(void)
{
    qwr_init();

    ble_uarts_init_t uarts_init_cfg = {0};
    uarts_init_cfg.data_handler = uarts_data_handler;

    ret_code_t err_code = ble_uarts_init(&m_uarts, &uarts_init_cfg);
    APP_ERROR_CHECK(err_code);

    status_char_init();
    printf("UART service ready\r\n");
}

static void conn_params_error_handler(uint32_t nrf_error)
{
    APP_ERROR_HANDLER(nrf_error);
}

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

static void advertising_init(void)
{
    ble_advertising_init_t init;
    memset(&init, 0, sizeof(init));

    static ble_uuid_t adv_uuids[] = {
        { BLE_UUID_UARTS_SERVICE, UARTS_SERVICE_UUID_TYPE }
    };

    init.advdata.name_type               = BLE_ADVDATA_FULL_NAME;
    init.advdata.include_appearance      = false;
    init.advdata.flags                   = BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE;
    init.advdata.uuids_complete.uuid_cnt = sizeof(adv_uuids) / sizeof(adv_uuids[0]);
    init.advdata.uuids_complete.p_uuids  = adv_uuids;

    init.config.ble_adv_fast_enabled  = true;
    init.config.ble_adv_fast_interval = APP_ADV_INTERVAL;
    init.config.ble_adv_fast_timeout  = APP_ADV_DURATION;
    init.evt_handler                  = NULL;

    ret_code_t err_code = ble_advertising_init(&m_advertising, &init);
    APP_ERROR_CHECK(err_code);

    ble_advertising_conn_cfg_tag_set(&m_advertising, APP_BLE_CONN_CFG_TAG);
}

static void reset_write_state(void)
{
    m_has_write_base = false;
    m_local_base_us = 0;
    m_host_base_us = 0;
    m_write_count = 0;
    m_last_status_value = 0;
    status_char_update(0);
}

static void on_conn_params_evt(ble_conn_params_evt_t * p_evt)
{
    if (p_evt->evt_type == BLE_CONN_PARAMS_EVT_FAILED)
    {
        ret_code_t err_code = sd_ble_gap_disconnect(m_conn_handle, BLE_HCI_CONN_INTERVAL_UNACCEPTABLE);
        APP_ERROR_CHECK(err_code);
    }
}

static void uarts_data_handler(ble_uarts_evt_t * p_evt)
{
    if (p_evt == NULL)
    {
        return;
    }

    switch (p_evt->type)
    {
        case BLE_UARTS_EVT_RX_DATA:
            if (p_evt->params.rx_data.length >= WRITE_PAYLOAD_SIZE)
            {
                uint64_t host_ts_us = read_le64(p_evt->params.rx_data.p_data);
                uint32_t local_now_us = timestamp_now_us();

                if (!m_has_write_base)
                {
                    m_local_base_us = local_now_us;
                    m_host_base_us = host_ts_us;
                    m_has_write_base = true;
                    printf("Write baseline set host=%llu us local=%lu us\r\n",
                           (unsigned long long)m_host_base_us,
                           (unsigned long)m_local_base_us);
                }

                uint32_t local_elapsed = local_now_us - m_local_base_us;
                uint64_t host_elapsed = host_ts_us - m_host_base_us;
                int64_t delta_us = (int64_t)local_elapsed - (int64_t)host_elapsed;

                m_write_count++;
                status_char_update((uint16_t)(m_write_count & 0xFFFF));

                printf("Write #%lu host=%llu us local=%lu us delta=%lld us\r\n",
                       (unsigned long)m_write_count,
                       (unsigned long long)host_elapsed,
                       (unsigned long)local_elapsed,
                       (long long)delta_us);
            }
            else
            {
                printf("RX short payload len=%u\r\n", (unsigned)p_evt->params.rx_data.length);
            }
            break;

        case BLE_NUS_EVT_COMM_STARTED:
            printf("Notifications enabled\r\n");
            break;

        case BLE_NUS_EVT_COMM_STOPPED:
            printf("Notifications disabled\r\n");
            break;

        default:
            break;
    }
}

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
            reset_write_state();

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
            reset_write_state();
            advertising_start();
            break;

        default:
            break;
    }
}

NRF_SDH_BLE_OBSERVER(m_ble_observer, APP_BLE_OBSERVER_PRIO, ble_evt_handler, NULL);

static void advertising_start(void)
{
    ret_code_t err_code = ble_advertising_start(&m_advertising, BLE_ADV_MODE_FAST);
    APP_ERROR_CHECK(err_code);
    printf("Advertising\r\n");
}

static void timers_init(void)
{
    ret_code_t err_code = app_timer_init();
    APP_ERROR_CHECK(err_code);
}

static void log_init(void)
{
    ret_code_t err_code = NRF_LOG_INIT(NULL);
    APP_ERROR_CHECK(err_code);

    NRF_LOG_DEFAULT_BACKENDS_INIT();
}

static void power_management_init(void)
{
    ret_code_t err_code = nrf_pwr_mgmt_init();
    APP_ERROR_CHECK(err_code);
}

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

static void idle_state_handle(void)
{
    if (NRF_LOG_PROCESS() == false)
    {
        nrf_pwr_mgmt_run();
    }
}

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

    printf("Stim/Exo write-test firmware ready\r\n");

    for (;;)
    {
        idle_state_handle();
    }
}
