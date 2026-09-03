#include "protocol.h"

#include <esp_log.h>

#define TAG "Protocol"

void Protocol::OnIncomingJson(std::function<void(const cJSON* root)> callback) {
    on_incoming_json_ = callback;
}

void Protocol::OnIncomingAudio(std::function<void(std::unique_ptr<AudioStreamPacket> packet)> callback) {
    on_incoming_audio_ = callback;
}

void Protocol::OnAudioChannelOpened(std::function<void()> callback) {
    on_audio_channel_opened_ = callback;
}

void Protocol::OnAudioChannelClosed(std::function<void()> callback) {
    on_audio_channel_closed_ = callback;
}

void Protocol::OnNetworkError(std::function<void(const std::string& message)> callback) {
    on_network_error_ = callback;
}

void Protocol::OnConnected(std::function<void()> callback) {
    on_connected_ = callback;
}

void Protocol::OnDisconnected(std::function<void()> callback) {
    on_disconnected_ = callback;
}

void Protocol::SetError(const std::string& message) {
    error_occurred_ = true;
    if (on_network_error_ != nullptr) {
        on_network_error_(message);
    }
}

void Protocol::SendAbortSpeaking(AbortReason reason, uint32_t metric_id,
        int64_t client_uptime_ms) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"abort\"";
    if (reason == kAbortReasonWakeWordDetected) {
        message += ",\"reason\":\"wake_word_detected\"";
    }
    if (metric_id != 0) {
        message += ",\"metric_id\":" + std::to_string(metric_id);
        message += ",\"client_uptime_ms\":" + std::to_string(client_uptime_ms);
    }
    message += "}";
    SendText(message);
}

void Protocol::SendBargeInMetric(uint32_t metric_id, uint32_t round_trip_ms,
        uint32_t local_clear_ms, int wifi_rssi_dbm, uint32_t free_sram_bytes,
        uint32_t min_free_sram_bytes, uint32_t uplink_frames_dropped) {
    std::string message = "{\"session_id\":\"" + session_id_ +
        "\",\"type\":\"client_metric\",\"name\":\"barge_in\"";
    message += ",\"metric_id\":" + std::to_string(metric_id);
    message += ",\"round_trip_ms\":" + std::to_string(round_trip_ms);
    message += ",\"local_clear_ms\":" + std::to_string(local_clear_ms);
    message += ",\"wifi_rssi_dbm\":" + std::to_string(wifi_rssi_dbm);
    message += ",\"free_sram_bytes\":" + std::to_string(free_sram_bytes);
    message += ",\"min_free_sram_bytes\":" + std::to_string(min_free_sram_bytes);
    message += ",\"uplink_frames_dropped\":" + std::to_string(uplink_frames_dropped);
    message += "}";
    SendText(message);
}

void Protocol::SendWakeWordDetected(const std::string& wake_word) {
    std::string json = "{\"session_id\":\"" + session_id_ + 
                      "\",\"type\":\"listen\",\"state\":\"detect\",\"text\":\"" + wake_word + "\"}";
    SendText(json);
}

void Protocol::SendStartListening(ListeningMode mode) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\"";
    message += ",\"type\":\"listen\",\"state\":\"start\"";
    if (mode == kListeningModeRealtime) {
        message += ",\"mode\":\"realtime\"";
    } else if (mode == kListeningModeAutoStop) {
        message += ",\"mode\":\"auto\"";
    } else {
        message += ",\"mode\":\"manual\"";
    }
    message += "}";
    SendText(message);
}

void Protocol::SendStopListening() {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"listen\",\"state\":\"stop\"}";
    SendText(message);
}

void Protocol::SendMcpMessage(const std::string& payload) {
    std::string message = "{\"session_id\":\"" + session_id_ + "\",\"type\":\"mcp\",\"payload\":" + payload + "}";
    SendText(message);
}

bool Protocol::IsTimeout() const {
    const int kTimeoutSeconds = 120;
    auto now = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration_cast<std::chrono::seconds>(now - last_incoming_time_);
    bool timeout = duration.count() > kTimeoutSeconds;
    if (timeout) {
        ESP_LOGE(TAG, "Channel timeout %ld seconds", (long)duration.count());
    }
    return timeout;
}
