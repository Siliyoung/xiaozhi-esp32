#ifndef LCD_DISPLAY_H
#define LCD_DISPLAY_H

#include "lvgl_display.h"
#include "gif/lvgl_gif.h"

#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <font_emoji.h>

#include <array>
#include <atomic>
#include <memory>

#define PREVIEW_IMAGE_DURATION_MS 5000


class LcdDisplay : public LvglDisplay {
protected:
    esp_lcd_panel_io_handle_t panel_io_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;
    
    lv_draw_buf_t draw_buf_;
    lv_obj_t* top_bar_ = nullptr;
    lv_obj_t* status_bar_ = nullptr;
    lv_obj_t* content_ = nullptr;
    lv_obj_t* container_ = nullptr;
    lv_obj_t* side_bar_ = nullptr;
    lv_obj_t* bottom_bar_ = nullptr;
    lv_obj_t* preview_image_ = nullptr;
    lv_obj_t* emoji_label_ = nullptr;
    lv_obj_t* emoji_image_ = nullptr;
    std::unique_ptr<LvglGif> gif_controller_ = nullptr;
    lv_obj_t* emoji_box_ = nullptr;
    lv_obj_t* chat_message_label_ = nullptr;
    esp_timer_handle_t preview_timer_ = nullptr;
    std::unique_ptr<LvglImage> preview_image_cached_ = nullptr;
    lv_obj_t* server_status_panel_ = nullptr;
    lv_obj_t* server_status_time_label_ = nullptr;
    lv_obj_t* server_cpu_value_label_ = nullptr;
    lv_obj_t* server_disk_value_label_ = nullptr;
    lv_obj_t* server_network_value_label_ = nullptr;
    lv_obj_t* server_cpu_chart_ = nullptr;
    lv_obj_t* server_disk_chart_ = nullptr;
    lv_obj_t* server_network_chart_ = nullptr;
    lv_chart_series_t* server_cpu_series_ = nullptr;
    lv_chart_series_t* server_disk_series_ = nullptr;
    lv_chart_series_t* server_network_rx_series_ = nullptr;
    lv_chart_series_t* server_network_tx_series_ = nullptr;
    std::array<float, 30> server_cpu_history_{};
    std::array<float, 30> server_network_rx_history_{};
    std::array<float, 30> server_network_tx_history_{};
    float server_cpu_scale_percent_ = 5.0f;
    float server_network_scale_kbps_ = 5.0f;
    lv_obj_t* pomodoro_panel_ = nullptr;
    lv_obj_t* pomodoro_arc_ = nullptr;
    lv_obj_t* pomodoro_time_label_ = nullptr;
    lv_obj_t* pomodoro_state_label_ = nullptr;
    lv_obj_t* pomodoro_name_label_ = nullptr;
    lv_obj_t* pomodoro_hint_label_ = nullptr;
    lv_obj_t* reminder_panel_ = nullptr;
    lv_obj_t* reminder_kind_label_ = nullptr;
    lv_obj_t* reminder_title_label_ = nullptr;
    lv_obj_t* reminder_time_label_ = nullptr;
    lv_obj_t* clock_panel_ = nullptr;
    lv_obj_t* clock_time_label_ = nullptr;
    lv_obj_t* clock_date_label_ = nullptr;
    lv_obj_t* clock_city_label_ = nullptr;
    lv_obj_t* clock_battery_label_ = nullptr;
    lv_obj_t* clock_condition_label_ = nullptr;
    lv_obj_t* clock_temperature_label_ = nullptr;
    lv_obj_t* clock_humidity_label_ = nullptr;
    bool hide_subtitle_ = false;  // Control whether to hide chat messages/subtitles

    void InitializeLcdThemes();
    virtual bool Lock(int timeout_ms = 0) override;
    virtual void Unlock() override;

protected:
    // Add protected constructor
    LcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width, int height);
    
public:
    ~LcdDisplay();
    virtual void SetEmotion(const char* emotion) override;
    virtual void SetChatMessage(const char* role, const char* content) override;
    virtual void ClearChatMessages() override;
    virtual void SetPreviewImage(std::unique_ptr<LvglImage> image) override;
    virtual void SetupUI() override;
    virtual void ShowServerStatus(float cpu_percent, float disk_percent, float network_rx_kbps, float network_tx_kbps, const char* sampled_at) override;
    virtual void HideServerStatus() override;
    virtual void ShowPomodoro(const char* state, int remaining_seconds, int total_seconds, const char* label) override;
    virtual void HidePomodoro() override;
    virtual void ShowReminder(const char* kind, const char* title, const char* time_text) override;
    virtual void HideReminder() override;
    virtual void ShowClock(const char* time_text, const char* date_text, int battery_percent,
        bool charging, const char* city, const char* condition, float temperature_c, int humidity_percent) override;
    virtual void HideClock() override;
    // Add theme switching function
    virtual void SetTheme(Theme* theme) override;
    
    // Set whether to hide chat messages/subtitles
    void SetHideSubtitle(bool hide);
};

// SPI LCD display
class SpiLcdDisplay : public LcdDisplay {
public:
    SpiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                  int width, int height, int offset_x, int offset_y,
                  bool mirror_x, bool mirror_y, bool swap_xy);
};

// RGB LCD display
class RgbLcdDisplay : public LcdDisplay {
public:
    RgbLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                  int width, int height, int offset_x, int offset_y,
                  bool mirror_x, bool mirror_y, bool swap_xy);
};

// MIPI LCD display
class MipiLcdDisplay : public LcdDisplay {
public:
    MipiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                   int width, int height, int offset_x, int offset_y,
                   bool mirror_x, bool mirror_y, bool swap_xy);
};

#endif // LCD_DISPLAY_H
