#include <esp_log.h>
#include <esp_err.h>
#include <string>
#include <cstdlib>
#include <cstring>
#include <font_awesome.h>

#include "display.h"
#include "board.h"
#include "application.h"
#include "audio_codec.h"
#include "settings.h"
#include "assets/lang_config.h"

#define TAG "Display"

Display::Display() {
}

Display::~Display() {
}

void Display::SetStatus(const char* status) {
    ESP_LOGW(TAG, "SetStatus: %s", status);
}

void Display::ShowNotification(const std::string &notification, int duration_ms) {
    ShowNotification(notification.c_str(), duration_ms);
}

void Display::ShowNotification(const char* notification, int duration_ms) {
    ESP_LOGW(TAG, "ShowNotification: %s", notification);
}

void Display::UpdateStatusBar(bool update_all) {
}


void Display::SetEmotion(const char* emotion) {
    ESP_LOGW(TAG, "SetEmotion: %s", emotion);
}

void Display::SetChatMessage(const char* role, const char* content) {
    ESP_LOGW(TAG, "Role:%s", role);
    ESP_LOGW(TAG, "     %s", content);
}

void Display::ClearChatMessages() {
    // Default empty implementation, override in subclasses if needed
}

void Display::SetTheme(Theme* theme) {
    current_theme_ = theme;
    Settings settings("display", true);
    settings.SetString("theme", theme->name());
}

void Display::ShowServerStatus(float cpu_percent, float disk_percent, float network_rx_kbps, float network_tx_kbps, const char* sampled_at) {
    ESP_LOGW(TAG, "Server status: CPU %.1f%%, disk %.1f%%, RX %.1f kbps, TX %.1f kbps",
        cpu_percent, disk_percent, network_rx_kbps, network_tx_kbps);
}

void Display::HideServerStatus() {
    // Displays without a graphical UI have nothing to restore.
}

void Display::ShowPomodoro(const char* state, int remaining_seconds, int total_seconds, const char* label) {
    ESP_LOGW(TAG, "Pomodoro: state=%s remaining=%d total=%d label=%s",
        state, remaining_seconds, total_seconds, label);
}

void Display::HidePomodoro() {
    // Displays without a graphical UI have nothing to restore.
}

void Display::ShowReminder(const char* kind, const char* title, const char* time_text) {
    ESP_LOGW(TAG, "Reminder: kind=%s title=%s time=%s", kind, title, time_text);
}

void Display::HideReminder() {
    // Displays without a graphical UI have nothing to restore.
}

void Display::ShowClock(const char* time_text, const char* date_text, int battery_percent,
        bool charging, const char* city, const char* condition, float temperature_c, int humidity_percent) {
    ESP_LOGW(TAG, "Clock: %s %s battery=%d charging=%d city=%s weather=%s temp=%.1f humidity=%d",
        time_text, date_text, battery_percent, charging, city, condition,
        temperature_c, humidity_percent);
}

void Display::HideClock() {
    // Displays without a graphical UI have nothing to restore.
}

void Display::SetPowerSaveMode(bool on) {
    ESP_LOGW(TAG, "SetPowerSaveMode: %d", on);
}
