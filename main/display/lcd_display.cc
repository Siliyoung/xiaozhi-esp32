#include "lcd_display.h"
#include "gif/lvgl_gif.h"
#include "settings.h"
#include "lvgl_theme.h"
#include "assets/lang_config.h"

#include <vector>
#include <algorithm>
#include <cmath>
#include <font_awesome.h>
#include <esp_log.h>
#include <esp_err.h>
#include <esp_lvgl_port.h>
#include <esp_psram.h>
#include <cstring>
#include <src/misc/cache/lv_cache.h>

#include "board.h"

#define TAG "LcdDisplay"

LV_FONT_DECLARE(BUILTIN_TEXT_FONT);
LV_FONT_DECLARE(BUILTIN_ICON_FONT);
LV_FONT_DECLARE(font_awesome_30_4);

namespace {

template <size_t N>
void PushChartSample(std::array<float, N>& history, float value) {
    std::move(history.begin() + 1, history.end(), history.begin());
    history.back() = value >= 0.0f ? value : 0.0f;
}

template <size_t N>
float RecentPeak(const std::array<float, N>& first, const std::array<float, N>* second = nullptr) {
    float peak = 0.0f;
    for (size_t index = 0; index < N; ++index) {
        peak = std::max(peak, first[index]);
        if (second != nullptr) {
            peak = std::max(peak, (*second)[index]);
        }
    }
    return peak;
}

float NiceChartUpper(float peak, float minimum, float maximum) {
    float target = std::clamp(peak * 1.25f, minimum, maximum);
    float step = 1.0f;
    if (target > 50.0f) {
        step = 10.0f;
    } else if (target > 25.0f) {
        step = 5.0f;
    } else if (target > 10.0f) {
        step = 2.5f;
    }
    return std::min(maximum, std::ceil(target / step) * step);
}

}  // namespace

void LcdDisplay::InitializeLcdThemes() {
    auto text_font = std::make_shared<LvglBuiltInFont>(&BUILTIN_TEXT_FONT);
    auto icon_font = std::make_shared<LvglBuiltInFont>(&BUILTIN_ICON_FONT);
    auto large_icon_font = std::make_shared<LvglBuiltInFont>(&font_awesome_30_4);

    // light theme
    auto light_theme = new LvglTheme("light");
    light_theme->set_background_color(lv_color_hex(0xFFFFFF));
    light_theme->set_text_color(lv_color_hex(0x000000));
    light_theme->set_chat_background_color(lv_color_hex(0xE0E0E0));
    light_theme->set_user_bubble_color(lv_color_hex(0x00FF00));
    light_theme->set_assistant_bubble_color(lv_color_hex(0xDDDDDD));
    light_theme->set_system_bubble_color(lv_color_hex(0xFFFFFF));
    light_theme->set_system_text_color(lv_color_hex(0x000000));
    light_theme->set_border_color(lv_color_hex(0x000000));
    light_theme->set_low_battery_color(lv_color_hex(0x000000));
    light_theme->set_text_font(text_font);
    light_theme->set_icon_font(icon_font);
    light_theme->set_large_icon_font(large_icon_font);

    // dark theme
    auto dark_theme = new LvglTheme("dark");
    dark_theme->set_background_color(lv_color_hex(0x000000));
    dark_theme->set_text_color(lv_color_hex(0xFFFFFF));
    dark_theme->set_chat_background_color(lv_color_hex(0x1F1F1F));
    dark_theme->set_user_bubble_color(lv_color_hex(0x00FF00));
    dark_theme->set_assistant_bubble_color(lv_color_hex(0x222222));
    dark_theme->set_system_bubble_color(lv_color_hex(0x000000));
    dark_theme->set_system_text_color(lv_color_hex(0xFFFFFF));
    dark_theme->set_border_color(lv_color_hex(0xFFFFFF));
    dark_theme->set_low_battery_color(lv_color_hex(0xFF0000));
    dark_theme->set_text_font(text_font);
    dark_theme->set_icon_font(icon_font);
    dark_theme->set_large_icon_font(large_icon_font);

    auto& theme_manager = LvglThemeManager::GetInstance();
    theme_manager.RegisterTheme("light", light_theme);
    theme_manager.RegisterTheme("dark", dark_theme);
}

LcdDisplay::LcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel, int width, int height)
    : panel_io_(panel_io), panel_(panel) {
    width_ = width;
    height_ = height;

    // Initialize LCD themes
    InitializeLcdThemes();

    // Load theme from settings
    Settings settings("display", false);
    std::string theme_name = settings.GetString("theme", "light");
    current_theme_ = LvglThemeManager::GetInstance().GetTheme(theme_name);

    // Create a timer to hide the preview image
    esp_timer_create_args_t preview_timer_args = {
        .callback = [](void* arg) {
            LcdDisplay* display = static_cast<LcdDisplay*>(arg);
            display->SetPreviewImage(nullptr);
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "preview_timer",
        .skip_unhandled_events = false,
    };
    esp_timer_create(&preview_timer_args, &preview_timer_);
}

SpiLcdDisplay::SpiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                           int width, int height, int offset_x, int offset_y, bool mirror_x, bool mirror_y, bool swap_xy)
    : LcdDisplay(panel_io, panel, width, height) {

    // draw white
    std::vector<uint16_t> buffer(width_, 0xFFFF);
    for (int y = 0; y < height_; y++) {
        esp_lcd_panel_draw_bitmap(panel_, 0, y, width_, y + 1, buffer.data());
    }

    // Set the display to on
    ESP_LOGI(TAG, "Turning display on");
    {
        esp_err_t __err = esp_lcd_panel_disp_on_off(panel_, true);
        if (__err == ESP_ERR_NOT_SUPPORTED) {
            ESP_LOGW(TAG, "Panel does not support disp_on_off; assuming ON");
        } else {
            ESP_ERROR_CHECK(__err);
        }
    }

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

#if CONFIG_SPIRAM
    // lv image cache, currently only PNG is supported
    size_t psram_size_mb = esp_psram_get_size() / 1024 / 1024;
    if (psram_size_mb >= 8) {
        lv_image_cache_resize(2 * 1024 * 1024, true);
        ESP_LOGI(TAG, "Use 2MB of PSRAM for image cache");
    } else if (psram_size_mb >= 2) {
        lv_image_cache_resize(512 * 1024, true);
        ESP_LOGI(TAG, "Use 512KB of PSRAM for image cache");
    }
#endif

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_priority = 1;
#if CONFIG_SOC_CPU_CORES_NUM > 1
    port_cfg.task_affinity = 1;
#endif
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t display_cfg = {
        .io_handle = panel_io_,
        .panel_handle = panel_,
        .control_handle = nullptr,
        .buffer_size = static_cast<uint32_t>(width_ * 20),
        .double_buffer = false,
        .trans_size = 0,
        .hres = static_cast<uint32_t>(width_),
        .vres = static_cast<uint32_t>(height_),
        .monochrome = false,
        .rotation = {
            .swap_xy = swap_xy,
            .mirror_x = mirror_x,
            .mirror_y = mirror_y,
        },
        .color_format = LV_COLOR_FORMAT_RGB565,
        .flags = {
            .buff_dma = 1,
            .buff_spiram = 0,
            .sw_rotate = 0,
            .swap_bytes = 1,
            .full_refresh = 0,
            .direct_mode = 0,
        },
    };

    display_ = lvgl_port_add_disp(&display_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add display");
        return;
    }

    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }
}


// RGB LCD implementation
RgbLcdDisplay::RgbLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                           int width, int height, int offset_x, int offset_y,
                           bool mirror_x, bool mirror_y, bool swap_xy)
    : LcdDisplay(panel_io, panel, width, height) {

    // draw white
    std::vector<uint16_t> buffer(width_, 0xFFFF);
    for (int y = 0; y < height_; y++) {
        esp_lcd_panel_draw_bitmap(panel_, 0, y, width_, y + 1, buffer.data());
    }

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    port_cfg.task_priority = 1;
    port_cfg.timer_period_ms = 50;
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t display_cfg = {
        .io_handle = panel_io_,
        .panel_handle = panel_,
        .buffer_size = static_cast<uint32_t>(width_ * 20),
        .double_buffer = true,
        .hres = static_cast<uint32_t>(width_),
        .vres = static_cast<uint32_t>(height_),
        .rotation = {
            .swap_xy = swap_xy,
            .mirror_x = mirror_x,
            .mirror_y = mirror_y,
        },
        .flags = {
            .buff_dma = 1,
            .swap_bytes = 0,
            .full_refresh = 1,
            .direct_mode = 1,
        },
    };

    const lvgl_port_display_rgb_cfg_t rgb_cfg = {
        .flags = {
            .bb_mode = true,
            .avoid_tearing = true,
        }
    };
    
    display_ = lvgl_port_add_disp_rgb(&display_cfg, &rgb_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add RGB display");
        return;
    }
    
    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }
}

MipiLcdDisplay::MipiLcdDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                            int width, int height,  int offset_x, int offset_y,
                            bool mirror_x, bool mirror_y, bool swap_xy)
    : LcdDisplay(panel_io, panel, width, height) {

    ESP_LOGI(TAG, "Initialize LVGL library");
    lv_init();

    ESP_LOGI(TAG, "Initialize LVGL port");
    lvgl_port_cfg_t port_cfg = ESP_LVGL_PORT_INIT_CONFIG();
    lvgl_port_init(&port_cfg);

    ESP_LOGI(TAG, "Adding LCD display");
    const lvgl_port_display_cfg_t disp_cfg = {
        .io_handle = panel_io,
        .panel_handle = panel,
        .control_handle = nullptr,
        .buffer_size = static_cast<uint32_t>(width_ * 50),
        .double_buffer = false,
        .hres = static_cast<uint32_t>(width_),
        .vres = static_cast<uint32_t>(height_),
        .monochrome = false,
        /* Rotation values must be same as used in esp_lcd for initial settings of the screen */
        .rotation = {
            .swap_xy = swap_xy,
            .mirror_x = mirror_x,
            .mirror_y = mirror_y,
        },
        .flags = {
            .buff_dma = true,
            .buff_spiram =false,
            .sw_rotate = true,
        },
    };

    const lvgl_port_display_dsi_cfg_t dpi_cfg = {
        .flags = {
            .avoid_tearing = false,
        }
    };
    display_ = lvgl_port_add_disp_dsi(&disp_cfg, &dpi_cfg);
    if (display_ == nullptr) {
        ESP_LOGE(TAG, "Failed to add display");
        return;
    }

    if (offset_x != 0 || offset_y != 0) {
        lv_display_set_offset(display_, offset_x, offset_y);
    }
}

LcdDisplay::~LcdDisplay() {
    SetPreviewImage(nullptr);
    
    // Clean up GIF controller
    if (gif_controller_) {
        gif_controller_->Stop();
        gif_controller_.reset();
    }
    
    if (preview_timer_ != nullptr) {
        esp_timer_stop(preview_timer_);
        esp_timer_delete(preview_timer_);
    }

    if (server_status_panel_ != nullptr) {
        lv_obj_del(server_status_panel_);
        server_status_panel_ = nullptr;
    }
    if (pomodoro_panel_ != nullptr) {
        lv_obj_del(pomodoro_panel_);
        pomodoro_panel_ = nullptr;
    }
    if (reminder_panel_ != nullptr) {
        lv_obj_del(reminder_panel_);
        reminder_panel_ = nullptr;
    }
    if (clock_panel_ != nullptr) {
        lv_obj_del(clock_panel_);
        clock_panel_ = nullptr;
    }
    if (preview_image_ != nullptr) {
        lv_obj_del(preview_image_);
    }
    if (chat_message_label_ != nullptr) {
        lv_obj_del(chat_message_label_);
    }
    if (emoji_label_ != nullptr) {
        lv_obj_del(emoji_label_);
    }
    if (emoji_image_ != nullptr) {
        lv_obj_del(emoji_image_);
    }
    if (emoji_box_ != nullptr) {
        lv_obj_del(emoji_box_);
    }
    if (content_ != nullptr) {
        lv_obj_del(content_);
    }
    if (bottom_bar_ != nullptr) {
        lv_obj_del(bottom_bar_);
    }
    if (status_bar_ != nullptr) {
        lv_obj_del(status_bar_);
    }
    if (top_bar_ != nullptr) {
        lv_obj_del(top_bar_);
    }
    if (side_bar_ != nullptr) {
        lv_obj_del(side_bar_);
    }
    if (container_ != nullptr) {
        lv_obj_del(container_);
    }
    if (display_ != nullptr) {
        lv_display_delete(display_);
    }

    if (panel_ != nullptr) {
        esp_lcd_panel_del(panel_);
    }
    if (panel_io_ != nullptr) {
        esp_lcd_panel_io_del(panel_io_);
    }
}

bool LcdDisplay::Lock(int timeout_ms) {
    return lvgl_port_lock(timeout_ms);
}

void LcdDisplay::Unlock() {
    lvgl_port_unlock();
}

#if CONFIG_USE_WECHAT_MESSAGE_STYLE
void LcdDisplay::SetupUI() {
    // Prevent duplicate calls - if already called, return early
    if (setup_ui_called_) {
        ESP_LOGW(TAG, "SetupUI() called multiple times, skipping duplicate call");
        return;
    }
    
    Display::SetupUI();  // Mark SetupUI as called
    DisplayLockGuard lock(this);

    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

    auto screen = lv_screen_active();
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_set_style_text_color(screen, lvgl_theme->text_color(), 0);
    lv_obj_set_style_bg_color(screen, lvgl_theme->background_color(), 0);

    /* Container */
    container_ = lv_obj_create(screen);
    lv_obj_set_size(container_, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_style_radius(container_, 0, 0);
    lv_obj_set_flex_flow(container_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(container_, 0, 0);
    lv_obj_set_style_border_width(container_, 0, 0);
    lv_obj_set_style_pad_row(container_, 0, 0);
    lv_obj_set_style_bg_color(container_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_border_color(container_, lvgl_theme->border_color(), 0);

    /* Layer 1: Top bar - for status icons */
    top_bar_ = lv_obj_create(container_);
    lv_obj_set_size(top_bar_, LV_HOR_RES, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(top_bar_, 0, 0);
    lv_obj_set_style_bg_opa(top_bar_, LV_OPA_50, 0);  // 50% opacity background
    lv_obj_set_style_bg_color(top_bar_, lvgl_theme->background_color(), 0);
    lv_obj_set_style_border_width(top_bar_, 0, 0);
    lv_obj_set_style_pad_all(top_bar_, 0, 0);
    lv_obj_set_style_pad_top(top_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(top_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_left(top_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_pad_right(top_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_flex_flow(top_bar_, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(top_bar_, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_scrollbar_mode(top_bar_, LV_SCROLLBAR_MODE_OFF);

    // Left icon
    network_label_ = lv_label_create(top_bar_);
    lv_label_set_text(network_label_, "");
    lv_obj_set_style_text_font(network_label_, icon_font, 0);
    lv_obj_set_style_text_color(network_label_, lvgl_theme->text_color(), 0);

    // Right icons container
    lv_obj_t* right_icons = lv_obj_create(top_bar_);
    lv_obj_set_size(right_icons, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(right_icons, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(right_icons, 0, 0);
    lv_obj_set_style_pad_all(right_icons, 0, 0);
    lv_obj_set_flex_flow(right_icons, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(right_icons, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    mute_label_ = lv_label_create(right_icons);
    lv_label_set_text(mute_label_, "");
    lv_obj_set_style_text_font(mute_label_, icon_font, 0);
    lv_obj_set_style_text_color(mute_label_, lvgl_theme->text_color(), 0);

    battery_label_ = lv_label_create(right_icons);
    lv_label_set_text(battery_label_, "");
    lv_obj_set_style_text_font(battery_label_, icon_font, 0);
    lv_obj_set_style_text_color(battery_label_, lvgl_theme->text_color(), 0);
    lv_obj_set_style_margin_left(battery_label_, lvgl_theme->spacing(2), 0);

    /* Layer 2: Status bar - for center text labels */
    status_bar_ = lv_obj_create(screen);
    lv_obj_set_size(status_bar_, LV_HOR_RES, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(status_bar_, 0, 0);
    lv_obj_set_style_bg_opa(status_bar_, LV_OPA_TRANSP, 0);  // Transparent background
    lv_obj_set_style_border_width(status_bar_, 0, 0);
    lv_obj_set_style_pad_all(status_bar_, 0, 0);
    lv_obj_set_style_pad_top(status_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(status_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_scrollbar_mode(status_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_layout(status_bar_, LV_LAYOUT_NONE, 0);  // Use absolute positioning
    lv_obj_align(status_bar_, LV_ALIGN_TOP_MID, 0, 0);  // Overlap with top_bar_

    notification_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(notification_label_, LV_HOR_RES * 0.8);
    lv_obj_set_style_text_align(notification_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(notification_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(notification_label_, "");
    lv_obj_align(notification_label_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);

    status_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(status_label_, LV_HOR_RES * 0.8);
    lv_label_set_long_mode(status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(status_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(status_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(status_label_, Lang::Strings::INITIALIZING);
    lv_obj_align(status_label_, LV_ALIGN_CENTER, 0, 0);
    
    /* Content - Chat area */
    content_ = lv_obj_create(container_);
    lv_obj_set_style_radius(content_, 0, 0);
    lv_obj_set_width(content_, LV_HOR_RES);
    lv_obj_set_flex_grow(content_, 1);
    lv_obj_set_style_pad_all(content_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_border_width(content_, 0, 0);
    lv_obj_set_style_bg_color(content_, lvgl_theme->chat_background_color(), 0); // Background for chat area

    // Enable scrolling for chat content
    lv_obj_set_scrollbar_mode(content_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_scroll_dir(content_, LV_DIR_VER);
    
    // Create a flex container for chat messages
    lv_obj_set_flex_flow(content_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(content_, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(content_, lvgl_theme->spacing(4), 0); // Space between messages

    // We'll create chat messages dynamically in SetChatMessage
    chat_message_label_ = nullptr;

    low_battery_popup_ = lv_obj_create(screen);
    lv_obj_set_scrollbar_mode(low_battery_popup_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_size(low_battery_popup_, LV_HOR_RES * 0.9, text_font->line_height * 2);
    lv_obj_align(low_battery_popup_, LV_ALIGN_BOTTOM_MID, 0, -lvgl_theme->spacing(4));
    lv_obj_set_style_bg_color(low_battery_popup_, lvgl_theme->low_battery_color(), 0);
    lv_obj_set_style_radius(low_battery_popup_, lvgl_theme->spacing(4), 0);
    low_battery_label_ = lv_label_create(low_battery_popup_);
    lv_label_set_text(low_battery_label_, Lang::Strings::BATTERY_NEED_CHARGE);
    lv_obj_set_style_text_color(low_battery_label_, lv_color_white(), 0);
    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);

    emoji_image_ = lv_img_create(screen);
    lv_obj_align(emoji_image_, LV_ALIGN_TOP_MID, 0, text_font->line_height + lvgl_theme->spacing(8));

    // Display AI logo while booting
    emoji_label_ = lv_label_create(screen);
    lv_obj_center(emoji_label_);
    lv_obj_set_style_text_font(emoji_label_, large_icon_font, 0);
    lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(emoji_label_, FONT_AWESOME_MICROCHIP_AI);
}
#if CONFIG_IDF_TARGET_ESP32P4
#define  MAX_MESSAGES 40
#else
#define  MAX_MESSAGES 20
#endif
void LcdDisplay::SetChatMessage(const char* role, const char* content) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "SetChatMessage('%s', '%s') called before SetupUI() - message will be lost!", role, content);
    }
    DisplayLockGuard lock(this);
    if (content_ == nullptr) {
        if (setup_ui_called_) {
            ESP_LOGW(TAG, "SetChatMessage('%s', '%s') failed: content_ is nullptr (SetupUI() was called but container not created)", role, content);
        }
        return;
    }
    
    // Check if message count exceeds limit
    uint32_t child_count = lv_obj_get_child_cnt(content_);
    if (child_count >= MAX_MESSAGES) {
        // Delete the oldest message (first child object)
        lv_obj_t* first_child = lv_obj_get_child(content_, 0);
        if (first_child != nullptr) {
            lv_obj_del(first_child);
            // Refresh child count after deletion
            child_count = lv_obj_get_child_cnt(content_);
        }
        // Scroll to the last message immediately (get last_child after deletion)
        if (child_count > 0) {
            lv_obj_t* last_child = lv_obj_get_child(content_, child_count - 1);
            if (last_child != nullptr && lv_obj_is_valid(last_child)) {
                lv_obj_scroll_to_view_recursive(last_child, LV_ANIM_OFF);
            }
        }
    }
    
    // Collapse system messages (if it's a system message, check if the last message is also a system message)
    if (strcmp(role, "system") == 0) {
        // Refresh child count to get accurate count after potential deletion above
        child_count = lv_obj_get_child_cnt(content_);
        if (child_count > 0) {
            // Get the last message container
            lv_obj_t* last_container = lv_obj_get_child(content_, child_count - 1);
            if (last_container != nullptr && lv_obj_is_valid(last_container) && lv_obj_get_child_cnt(last_container) > 0) {
                // Get the bubble inside the container
                lv_obj_t* last_bubble = lv_obj_get_child(last_container, 0);
                if (last_bubble != nullptr && lv_obj_is_valid(last_bubble)) {
                    // Check if bubble type is system message
                    void* bubble_type_ptr = lv_obj_get_user_data(last_bubble);
                    if (bubble_type_ptr != nullptr && strcmp((const char*)bubble_type_ptr, "system") == 0) {
                        // If the last message is also a system message, delete it
                        lv_obj_del(last_container);
                    }
                }
            }
        }
    } else {
        // Hide the centered AI logo
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }

    // Avoid empty message boxes
    if(strlen(content) == 0) {
        return;
    }

    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);

    // Create a message bubble
    lv_obj_t* msg_bubble = lv_obj_create(content_);
    lv_obj_set_style_radius(msg_bubble, 8, 0);
    lv_obj_set_scrollbar_mode(msg_bubble, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_border_width(msg_bubble, 0, 0);
    lv_obj_set_style_pad_all(msg_bubble, lvgl_theme->spacing(4), 0);

    // Create the message text
    lv_obj_t* msg_text = lv_label_create(msg_bubble);
    lv_label_set_text(msg_text, content);
    
    // Calculate bubble width constraints
    lv_coord_t max_width = LV_HOR_RES * 85 / 100 - 16;  // 85% of screen width
    lv_coord_t min_width = 20;  
    
    // Let LVGL calculate the natural text width first
    lv_obj_set_width(msg_text, LV_SIZE_CONTENT);
    lv_obj_update_layout(msg_text);
    lv_coord_t text_width = lv_obj_get_width(msg_text);
    
    // Ensure text width is not less than minimum width
    if (text_width < min_width) {
        text_width = min_width;
    }

    // Constrain to max width
    lv_coord_t bubble_width = (text_width < max_width) ? text_width : max_width;
    
    // Set message text width
    lv_obj_set_width(msg_text, bubble_width);
    lv_label_set_long_mode(msg_text, LV_LABEL_LONG_WRAP);

    // Set bubble width
    lv_obj_set_width(msg_bubble, bubble_width);
    lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);

    // Set alignment and style based on message role
    if (strcmp(role, "user") == 0) {
        // User messages are right-aligned with green background
        lv_obj_set_style_bg_color(msg_bubble, lvgl_theme->user_bubble_color(), 0);
        lv_obj_set_style_bg_opa(msg_bubble, LV_OPA_70, 0);
        // Set text color for contrast
        lv_obj_set_style_text_color(msg_text, lvgl_theme->text_color(), 0);
        
        // Set custom attribute to mark bubble type
        lv_obj_set_user_data(msg_bubble, (void*)"user");
        
        // Set appropriate width for content
        lv_obj_set_width(msg_bubble, LV_SIZE_CONTENT);
        lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);
        
        // Don't grow
        lv_obj_set_style_flex_grow(msg_bubble, 0, 0);
    } else if (strcmp(role, "assistant") == 0) {
        // Assistant messages are left-aligned with white background
        lv_obj_set_style_bg_color(msg_bubble, lvgl_theme->assistant_bubble_color(), 0);
        lv_obj_set_style_bg_opa(msg_bubble, LV_OPA_70, 0);
        // Set text color for contrast
        lv_obj_set_style_text_color(msg_text, lvgl_theme->text_color(), 0);
        
        // Set custom attribute to mark bubble type
        lv_obj_set_user_data(msg_bubble, (void*)"assistant");
        
        // Set appropriate width for content
        lv_obj_set_width(msg_bubble, LV_SIZE_CONTENT);
        lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);
        
        // Don't grow
        lv_obj_set_style_flex_grow(msg_bubble, 0, 0);
    } else if (strcmp(role, "system") == 0) {
        // System messages are center-aligned with light gray background
        lv_obj_set_style_bg_color(msg_bubble, lvgl_theme->system_bubble_color(), 0);
        lv_obj_set_style_bg_opa(msg_bubble, LV_OPA_70, 0);
        // Set text color for contrast
        lv_obj_set_style_text_color(msg_text, lvgl_theme->system_text_color(), 0);
        
        // Set custom attribute to mark bubble type
        lv_obj_set_user_data(msg_bubble, (void*)"system");
        
        // Set appropriate width for content
        lv_obj_set_width(msg_bubble, LV_SIZE_CONTENT);
        lv_obj_set_height(msg_bubble, LV_SIZE_CONTENT);
        
        // Don't grow
        lv_obj_set_style_flex_grow(msg_bubble, 0, 0);
    }
    
    // Create a full-width container for user messages to ensure right alignment
    if (strcmp(role, "user") == 0) {
        // Create a full-width container
        lv_obj_t* container = lv_obj_create(content_);
        lv_obj_set_width(container, LV_HOR_RES);
        lv_obj_set_height(container, LV_SIZE_CONTENT);
        
        // Make container transparent and borderless
        lv_obj_set_style_bg_opa(container, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(container, 0, 0);
        lv_obj_set_style_pad_all(container, 0, 0);
        
        // Move the message bubble into this container
        lv_obj_set_parent(msg_bubble, container);
        
        // Right align the bubble in the container
        lv_obj_align(msg_bubble, LV_ALIGN_RIGHT_MID, -25, 0);
        
        // Auto-scroll to this container
        lv_obj_scroll_to_view_recursive(container, LV_ANIM_ON);
    } else if (strcmp(role, "system") == 0) {
        // Create full-width container for system messages to ensure center alignment
        lv_obj_t* container = lv_obj_create(content_);
        lv_obj_set_width(container, LV_HOR_RES);
        lv_obj_set_height(container, LV_SIZE_CONTENT);
        
        lv_obj_set_style_bg_opa(container, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(container, 0, 0);
        lv_obj_set_style_pad_all(container, 0, 0);
        
        lv_obj_set_parent(msg_bubble, container);
        lv_obj_align(msg_bubble, LV_ALIGN_CENTER, 0, 0);
        lv_obj_scroll_to_view_recursive(container, LV_ANIM_ON);
    } else {
        // For assistant messages
        // Left align assistant messages
        lv_obj_align(msg_bubble, LV_ALIGN_LEFT_MID, 0, 0);

        // Auto-scroll to the message bubble
        lv_obj_scroll_to_view_recursive(msg_bubble, LV_ANIM_ON);
    }
    
    // Store reference to the latest message label
    chat_message_label_ = msg_text;
}

void LcdDisplay::SetPreviewImage(std::unique_ptr<LvglImage> image) {
    DisplayLockGuard lock(this);
    if (content_ == nullptr) {
        return;
    }

    if (image == nullptr) {
        return;
    }
    
    auto lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    // Create a message bubble for image preview
    lv_obj_t* img_bubble = lv_obj_create(content_);
    lv_obj_set_style_radius(img_bubble, 8, 0);
    lv_obj_set_scrollbar_mode(img_bubble, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_border_width(img_bubble, 0, 0);
    lv_obj_set_style_pad_all(img_bubble, lvgl_theme->spacing(4), 0);
    
    // Set image bubble background color (similar to system message)
    lv_obj_set_style_bg_color(img_bubble, lvgl_theme->assistant_bubble_color(), 0);
    lv_obj_set_style_bg_opa(img_bubble, LV_OPA_70, 0);
    
    // Set custom attribute to mark bubble type
    lv_obj_set_user_data(img_bubble, (void*)"image");

    // Create the image object inside the bubble
    lv_obj_t* preview_image = lv_image_create(img_bubble);
    
    // Calculate appropriate size for the image
    lv_coord_t max_width = LV_HOR_RES * 70 / 100;  // 70% of screen width
    lv_coord_t max_height = LV_VER_RES * 50 / 100; // 50% of screen height
    
    // Calculate zoom factor to fit within maximum dimensions
    auto img_dsc = image->image_dsc();
    lv_coord_t img_width = img_dsc->header.w;
    lv_coord_t img_height = img_dsc->header.h;
    if (img_width == 0 || img_height == 0) {
        img_width = max_width;
        img_height = max_height;
        ESP_LOGW(TAG, "Invalid image dimensions: %ld x %ld, using default dimensions: %ld x %ld", img_width, img_height, max_width, max_height);
    }
    
    lv_coord_t zoom_w = (max_width * 256) / img_width;
    lv_coord_t zoom_h = (max_height * 256) / img_height;
    lv_coord_t zoom = (zoom_w < zoom_h) ? zoom_w : zoom_h;
    
    // Ensure zoom doesn't exceed 256 (100%)
    if (zoom > 256) zoom = 256;
    
    // Set image properties
    lv_image_set_src(preview_image, img_dsc);
    lv_image_set_scale(preview_image, zoom);
    
    // Add event handler to clean up LvglImage when image is deleted
    // We need to transfer ownership of the unique_ptr to the event callback
    LvglImage* raw_image = image.release(); // Release ownership of smart pointer
    lv_obj_add_event_cb(preview_image, [](lv_event_t* e) {
        LvglImage* img = (LvglImage*)lv_event_get_user_data(e);
        if (img != nullptr) {
            delete img; // Properly release memory by deleting LvglImage object
        }
    }, LV_EVENT_DELETE, (void*)raw_image);
    
    // Calculate actual scaled image dimensions
    lv_coord_t scaled_width = (img_width * zoom) / 256;
    lv_coord_t scaled_height = (img_height * zoom) / 256;
    
    // Set bubble size to be 16 pixels larger than the image (8 pixels on each side)
    lv_obj_set_width(img_bubble, scaled_width + 16);
    lv_obj_set_height(img_bubble, scaled_height + 16);
    
    // Don't grow in flex layout
    lv_obj_set_style_flex_grow(img_bubble, 0, 0);
    
    // Center the image within the bubble
    lv_obj_center(preview_image);
    
    // Left align the image bubble like assistant messages
    lv_obj_align(img_bubble, LV_ALIGN_LEFT_MID, 0, 0);

    // Auto-scroll to the image bubble
    lv_obj_scroll_to_view_recursive(img_bubble, LV_ANIM_ON);
}

void LcdDisplay::ClearChatMessages() {
    DisplayLockGuard lock(this);
    if (content_ == nullptr) {
        return;
    }
    
    // Use lv_obj_clean to delete all children of content_ (chat message bubbles)
    lv_obj_clean(content_);
    
    // Reset chat_message_label_ as it has been deleted
    chat_message_label_ = nullptr;
    
    // Show the centered AI logo (emoji_label_) again
    if (emoji_label_ != nullptr) {
        lv_obj_remove_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }
    
    ESP_LOGI(TAG, "Chat messages cleared");
}
#else
void LcdDisplay::SetupUI() {
    // Prevent duplicate calls - if already called, return early
    if (setup_ui_called_) {
        ESP_LOGW(TAG, "SetupUI() called multiple times, skipping duplicate call");
        return;
    }
    
    Display::SetupUI();  // Mark SetupUI as called
    DisplayLockGuard lock(this);
    LvglTheme* lvgl_theme = static_cast<LvglTheme*>(current_theme_);
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

    auto screen = lv_screen_active();
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_set_style_text_color(screen, lvgl_theme->text_color(), 0);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x071423), 0);

    /* Container - used as background */
    container_ = lv_obj_create(screen);
    lv_obj_set_size(container_, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_style_radius(container_, 0, 0);
    lv_obj_set_style_pad_all(container_, 0, 0);
    lv_obj_set_style_border_width(container_, 0, 0);
    lv_obj_set_style_bg_color(container_, lv_color_hex(0x071423), 0);
    lv_obj_set_style_bg_grad_color(container_, lv_color_hex(0x102A3A), 0);
    lv_obj_set_style_bg_grad_dir(container_, LV_GRAD_DIR_VER, 0);
    lv_obj_set_style_border_color(container_, lv_color_hex(0x203E50), 0);

    /* Bottom layer: emoji_box_ - centered display */
    emoji_box_ = lv_obj_create(screen);
    lv_obj_set_size(emoji_box_, 220, 220);
    lv_obj_set_style_bg_opa(emoji_box_, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(emoji_box_, 0, 0);
    lv_obj_set_style_border_width(emoji_box_, 0, 0);
    lv_obj_align(emoji_box_, LV_ALIGN_CENTER, 0, 0);

    emoji_label_ = lv_label_create(emoji_box_);
    lv_obj_set_style_text_font(emoji_label_, large_icon_font, 0);
    lv_obj_set_style_text_color(emoji_label_, lvgl_theme->text_color(), 0);
    lv_label_set_text(emoji_label_, FONT_AWESOME_MICROCHIP_AI);

    emoji_image_ = lv_img_create(emoji_box_);
    lv_obj_center(emoji_image_);
    lv_image_set_scale(emoji_image_, 384);
    lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);

    /* Middle layer: preview_image_ - centered display */
    preview_image_ = lv_image_create(screen);
    lv_obj_set_size(preview_image_, width_ / 2, height_ / 2);
    lv_obj_align(preview_image_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);

    /* Layer 1: Top bar - for status icons */
    top_bar_ = lv_obj_create(screen);
    lv_obj_set_size(top_bar_, LV_HOR_RES, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(top_bar_, 0, 0);
    lv_obj_set_style_bg_opa(top_bar_, LV_OPA_50, 0);  // 50% opacity background
    lv_obj_set_style_bg_color(top_bar_, lv_color_hex(0x0D2433), 0);
    lv_obj_set_style_border_width(top_bar_, 0, 0);
    lv_obj_set_style_pad_all(top_bar_, 0, 0);
    lv_obj_set_style_pad_top(top_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(top_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_left(top_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_pad_right(top_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_flex_flow(top_bar_, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(top_bar_, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_scrollbar_mode(top_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_align(top_bar_, LV_ALIGN_TOP_MID, 0, 0);

    // Left icon
    network_label_ = lv_label_create(top_bar_);
    lv_label_set_text(network_label_, "");
    lv_obj_set_style_text_font(network_label_, icon_font, 0);
    lv_obj_set_style_text_color(network_label_, lv_color_hex(0xD9EDF4), 0);

    // Right icons container
    lv_obj_t* right_icons = lv_obj_create(top_bar_);
    lv_obj_set_size(right_icons, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(right_icons, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(right_icons, 0, 0);
    lv_obj_set_style_pad_all(right_icons, 0, 0);
    lv_obj_set_flex_flow(right_icons, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(right_icons, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    mute_label_ = lv_label_create(right_icons);
    lv_label_set_text(mute_label_, "");
    lv_obj_set_style_text_font(mute_label_, icon_font, 0);
    lv_obj_set_style_text_color(mute_label_, lv_color_hex(0xD9EDF4), 0);

    battery_label_ = lv_label_create(right_icons);
    lv_label_set_text(battery_label_, "");
    lv_obj_set_style_text_font(battery_label_, icon_font, 0);
    lv_obj_set_style_text_color(battery_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_margin_left(battery_label_, lvgl_theme->spacing(2), 0);

    /* Layer 2: Status bar - for center text labels */
    status_bar_ = lv_obj_create(screen);
    lv_obj_set_size(status_bar_, LV_HOR_RES, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(status_bar_, 0, 0);
    lv_obj_set_style_bg_opa(status_bar_, LV_OPA_TRANSP, 0);  // Transparent background
    lv_obj_set_style_border_width(status_bar_, 0, 0);
    lv_obj_set_style_pad_all(status_bar_, 0, 0);
    lv_obj_set_style_pad_top(status_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_style_pad_bottom(status_bar_, lvgl_theme->spacing(2), 0);
    lv_obj_set_scrollbar_mode(status_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_style_layout(status_bar_, LV_LAYOUT_NONE, 0);  // Use absolute positioning
    lv_obj_align(status_bar_, LV_ALIGN_TOP_MID, 0, 0);  // Overlap with top_bar_

    notification_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(notification_label_, LV_HOR_RES * 0.75);
    lv_obj_set_style_text_align(notification_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(notification_label_, lv_color_hex(0xD9EDF4), 0);
    lv_label_set_text(notification_label_, "");
    lv_obj_align(notification_label_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);

    status_label_ = lv_label_create(status_bar_);
    lv_obj_set_width(status_label_, LV_HOR_RES * 0.75);
    lv_label_set_long_mode(status_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(status_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(status_label_, lv_color_hex(0xD9EDF4), 0);
    lv_label_set_text(status_label_, Lang::Strings::INITIALIZING);
    lv_obj_align(status_label_, LV_ALIGN_CENTER, 0, 0);

#if CONFIG_USE_MULTILINE_CHAT_MESSAGE
    /* Bottom bar - auto height, grows upward with wrapped text */
    bottom_bar_ = lv_obj_create(screen);
    lv_obj_set_width(bottom_bar_, LV_HOR_RES);
    lv_obj_set_height(bottom_bar_, LV_SIZE_CONTENT);
    lv_obj_set_style_radius(bottom_bar_, 0, 0);
    lv_obj_set_style_bg_color(bottom_bar_, lv_color_hex(0x0D2433), 0);
    lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_50, 0);
    lv_obj_set_style_text_color(bottom_bar_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_pad_all(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_border_width(bottom_bar_, 0, 0);
    lv_obj_set_scrollbar_mode(bottom_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, 0);

    /* chat_message_label_ placed in bottom_bar_, multiline wrapped display */
    chat_message_label_ = lv_label_create(bottom_bar_);
    lv_label_set_text(chat_message_label_, "");
    lv_obj_set_width(chat_message_label_, LV_HOR_RES - lvgl_theme->spacing(8));
    lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(chat_message_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(chat_message_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_align(chat_message_label_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);  // Hide until there is content
#else
    /* Top layer: Bottom bar - fixed height at bottom */
    bottom_bar_ = lv_obj_create(screen);
    lv_obj_set_size(bottom_bar_, LV_HOR_RES, text_font->line_height + lvgl_theme->spacing(8));
    lv_obj_set_style_radius(bottom_bar_, 0, 0);
    lv_obj_set_style_bg_color(bottom_bar_, lv_color_hex(0x0D2433), 0);
    lv_obj_set_style_text_color(bottom_bar_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_pad_all(bottom_bar_, 0, 0);
    lv_obj_set_style_pad_left(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_pad_right(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_border_width(bottom_bar_, 0, 0);
    lv_obj_set_scrollbar_mode(bottom_bar_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, 0);

    /* chat_message_label_ placed in bottom_bar_, single-line horizontal scroll */
    chat_message_label_ = lv_label_create(bottom_bar_);
    lv_label_set_text(chat_message_label_, "");
    lv_obj_set_width(chat_message_label_, LV_HOR_RES - lvgl_theme->spacing(8));
    lv_label_set_long_mode(chat_message_label_, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_align(chat_message_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(chat_message_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_align(chat_message_label_, LV_ALIGN_CENTER, 0, 0);

    // Start scrolling after a delay (short text won't scroll)
    static lv_anim_t a;
    lv_anim_init(&a);
    lv_anim_set_delay(&a, 1000);
    lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
    lv_obj_set_style_anim(chat_message_label_, &a, LV_PART_MAIN);
    lv_obj_set_style_anim_duration(chat_message_label_, lv_anim_speed_clamped(60, 300, 60000), LV_PART_MAIN);
    lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);  // Hide until there is content
#endif

    low_battery_popup_ = lv_obj_create(screen);
    lv_obj_set_scrollbar_mode(low_battery_popup_, LV_SCROLLBAR_MODE_OFF);
    lv_obj_set_size(low_battery_popup_, LV_HOR_RES * 0.9, text_font->line_height * 2);
    lv_obj_align(low_battery_popup_, LV_ALIGN_BOTTOM_MID, 0, -lvgl_theme->spacing(4));
    lv_obj_set_style_bg_color(low_battery_popup_, lvgl_theme->low_battery_color(), 0);
    lv_obj_set_style_radius(low_battery_popup_, lvgl_theme->spacing(4), 0);
    
    low_battery_label_ = lv_label_create(low_battery_popup_);
    lv_label_set_text(low_battery_label_, Lang::Strings::BATTERY_NEED_CHARGE);
    lv_obj_set_style_text_color(low_battery_label_, lv_color_white(), 0);
    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);
}

void LcdDisplay::SetPreviewImage(std::unique_ptr<LvglImage> image) {
    DisplayLockGuard lock(this);
    if (preview_image_ == nullptr) {
        ESP_LOGE(TAG, "Preview image is not initialized");
        return;
    }

    if (image == nullptr) {
        esp_timer_stop(preview_timer_);
        lv_obj_remove_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
        preview_image_cached_.reset();
        if (gif_controller_) {
            gif_controller_->Start();
        }
        return;
    }

    preview_image_cached_ = std::move(image);
    auto img_dsc = preview_image_cached_->image_dsc();
    lv_image_set_src(preview_image_, img_dsc);
    if (img_dsc->header.w > 0 && img_dsc->header.h > 0) {
        // zoom factor 0.5
        lv_image_set_scale(preview_image_, 128 * width_ / img_dsc->header.w);
    }

    // Hide emoji_box_
    if (gif_controller_) {
        gif_controller_->Stop();
    }
    lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_remove_flag(preview_image_, LV_OBJ_FLAG_HIDDEN);
    esp_timer_stop(preview_timer_);
    ESP_ERROR_CHECK(esp_timer_start_once(preview_timer_, PREVIEW_IMAGE_DURATION_MS * 1000));
}

void LcdDisplay::SetChatMessage(const char* role, const char* content) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "SetChatMessage('%s', '%s') called before SetupUI() - message will be lost!", role, content);
    }
    DisplayLockGuard lock(this);
    if (chat_message_label_ == nullptr) {
        if (setup_ui_called_) {
            ESP_LOGW(TAG, "SetChatMessage('%s', '%s') failed: chat_message_label_ is nullptr (SetupUI() was called but label not created)", role, content);
        }
        return;
    }
    lv_label_set_text(chat_message_label_, content);
    // Show bottom_bar_ only when there is content (and subtitle is not globally hidden)
    if (bottom_bar_ != nullptr) {
        if (content == nullptr || content[0] == '\0') {
            lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        } else if (!hide_subtitle_) {
            lv_obj_remove_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        }
    }
#if CONFIG_USE_MULTILINE_CHAT_MESSAGE
    // Re-align bottom_bar_ after text change so it stays anchored to the bottom
    // as its height adapts to the wrapped content.
    if (bottom_bar_ != nullptr) {
        lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, 0);
    }
#endif
}

void LcdDisplay::ClearChatMessages() {
    DisplayLockGuard lock(this);
    // In non-wechat mode, just clear the chat message label and hide the bar
    if (chat_message_label_ != nullptr) {
        lv_label_set_text(chat_message_label_, "");
    }
    if (bottom_bar_ != nullptr) {
        lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
    }
}
#endif

void LcdDisplay::SetEmotion(const char* emotion) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "SetEmotion('%s') called before SetupUI() - emotion will not be displayed!", emotion);
    }
    if (emoji_image_ == nullptr) {
        if (setup_ui_called_) {
            ESP_LOGW(TAG, "SetEmotion('%s') failed: emoji_image_ is nullptr (SetupUI() was called but emoji image not created)", emotion);
        }
        return;
    }

    auto emoji_collection = static_cast<LvglTheme*>(current_theme_)->emoji_collection();
    auto image = emoji_collection != nullptr ? emoji_collection->GetEmojiImage(emotion) : nullptr;
    if (image == nullptr) {
        const char* utf8 = font_awesome_get_utf8(emotion);
        if (utf8 != nullptr && emoji_label_ != nullptr) {
            DisplayLockGuard lock(this);
            if (gif_controller_) {
                gif_controller_->Stop();
                gif_controller_.reset();
            }
            lv_label_set_text(emoji_label_, utf8);
            lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
        }
        return;
    }

    DisplayLockGuard lock(this);
    // Stop any running GIF animation in the same lock scope as setting new image
    // to prevent LVGL from accessing freed image data between operations
    if (gif_controller_) {
        gif_controller_->Stop();
        gif_controller_.reset();
    }
    if (image->IsGif()) {
        // Create new GIF controller
        gif_controller_ = std::make_unique<LvglGif>(image->image_dsc());
        
        if (gif_controller_->IsLoaded()) {
            // Set up frame update callback
            gif_controller_->SetFrameCallback([this]() {
                lv_image_set_src(emoji_image_, gif_controller_->image_dsc());
            });
            
            // Set initial frame and start animation
            lv_image_set_src(emoji_image_, gif_controller_->image_dsc());
            gif_controller_->Start();
            
            // Show GIF, hide others
            lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
        } else {
            ESP_LOGE(TAG, "Failed to load GIF for emotion: %s", emotion);
            gif_controller_.reset();
        }
    } else {
        lv_image_set_src(emoji_image_, image->image_dsc());
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }

#if CONFIG_USE_WECHAT_MESSAGE_STYLE
    // In WeChat message style, if emotion is neutral, don't display it
    uint32_t child_count = lv_obj_get_child_cnt(content_);
    if (strcmp(emotion, "neutral") == 0 && child_count > 0) {
        // Stop GIF animation if running
        if (gif_controller_) {
            gif_controller_->Stop();
            gif_controller_.reset();
        }
        
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }
#endif
}

void LcdDisplay::SetTheme(Theme* theme) {
    DisplayLockGuard lock(this);
    
    auto lvgl_theme = static_cast<LvglTheme*>(theme);
    
    // Get the active screen
    lv_obj_t* screen = lv_screen_active();

    // Set font
    auto text_font = lvgl_theme->text_font()->font();
    auto icon_font = lvgl_theme->icon_font()->font();
    auto large_icon_font = lvgl_theme->large_icon_font()->font();

    if (text_font->line_height >= 40) {
        lv_obj_set_style_text_font(mute_label_, large_icon_font, 0);
        lv_obj_set_style_text_font(battery_label_, large_icon_font, 0);
        lv_obj_set_style_text_font(network_label_, large_icon_font, 0);
    } else {
        lv_obj_set_style_text_font(mute_label_, icon_font, 0);
        lv_obj_set_style_text_font(battery_label_, icon_font, 0);
        lv_obj_set_style_text_font(network_label_, icon_font, 0);
    }

    // Set parent text color
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_set_style_text_color(screen, lvgl_theme->text_color(), 0);

    // Keep the conversation page aligned with the dark tool/clock pages.
    lv_obj_set_style_bg_image_src(container_, nullptr, 0);
    lv_obj_set_style_bg_color(container_, lv_color_hex(0x071423), 0);
    lv_obj_set_style_bg_grad_color(container_, lv_color_hex(0x102A3A), 0);
    lv_obj_set_style_bg_grad_dir(container_, LV_GRAD_DIR_VER, 0);
    
    // Update top bar background color with 50% opacity
    if (top_bar_ != nullptr) {
        lv_obj_set_style_bg_opa(top_bar_, LV_OPA_50, 0);
        lv_obj_set_style_bg_color(top_bar_, lv_color_hex(0x0D2433), 0);
    }
    
    // Update status bar elements
    lv_obj_set_style_text_color(network_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_text_color(status_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_text_color(notification_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_text_color(mute_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_text_color(battery_label_, lv_color_hex(0xD9EDF4), 0);
    lv_obj_set_style_text_color(emoji_label_, lv_color_hex(0xD9EDF4), 0);

    // If we have the chat message style, update all message bubbles
#if CONFIG_USE_WECHAT_MESSAGE_STYLE
    // Set content background opacity
    lv_obj_set_style_bg_opa(content_, LV_OPA_TRANSP, 0);

    // Iterate through all children of content (message containers or bubbles)
    uint32_t child_count = lv_obj_get_child_cnt(content_);
    for (uint32_t i = 0; i < child_count; i++) {
        lv_obj_t* obj = lv_obj_get_child(content_, i);
        if (obj == nullptr) continue;
        
        lv_obj_t* bubble = nullptr;
        
        // Check if this object is a container or bubble
        // If it's a container (user or system message), get its child as bubble
        // If it's a bubble (assistant message), use it directly
        if (lv_obj_get_child_cnt(obj) > 0) {
            // Might be a container, check if it's a user or system message container
            // User and system message containers are transparent
            lv_opa_t bg_opa = lv_obj_get_style_bg_opa(obj, LV_PART_MAIN);
            if (bg_opa == LV_OPA_TRANSP) {
                // This is a user or system message container
                bubble = lv_obj_get_child(obj, 0);
            } else {
                // This might be an assistant message bubble itself
                bubble = obj;
            }
        } else {
            // No child elements, might be other UI elements, skip
            continue;
        }
        
        if (bubble == nullptr) continue;
        
        // Use saved user data to identify bubble type
        void* bubble_type_ptr = lv_obj_get_user_data(bubble);
        if (bubble_type_ptr != nullptr) {
            const char* bubble_type = static_cast<const char*>(bubble_type_ptr);
            
            // Apply correct color based on bubble type
            if (strcmp(bubble_type, "user") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->user_bubble_color(), 0);
            } else if (strcmp(bubble_type, "assistant") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->assistant_bubble_color(), 0); 
            } else if (strcmp(bubble_type, "system") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->system_bubble_color(), 0);
            } else if (strcmp(bubble_type, "image") == 0) {
                lv_obj_set_style_bg_color(bubble, lvgl_theme->system_bubble_color(), 0);
            }
            
            // Update border color
            lv_obj_set_style_border_color(bubble, lvgl_theme->border_color(), 0);
            
            // Update text color for the message
            if (lv_obj_get_child_cnt(bubble) > 0) {
                lv_obj_t* text = lv_obj_get_child(bubble, 0);
                if (text != nullptr) {
                    // Set text color based on bubble type
                    if (strcmp(bubble_type, "system") == 0) {
                        lv_obj_set_style_text_color(text, lvgl_theme->system_text_color(), 0);
                    } else {
                        lv_obj_set_style_text_color(text, lvgl_theme->text_color(), 0);
                    }
                }
            }
        } else {
            ESP_LOGW(TAG, "child[%lu] Bubble type is not found", i);
        }
    }
#else
    // Simple UI mode - just update the main chat message
    if (chat_message_label_ != nullptr) {
        lv_obj_set_style_text_color(chat_message_label_, lv_color_hex(0xD9EDF4), 0);
    }
    
    if (emoji_label_ != nullptr) {
        lv_obj_set_style_text_color(emoji_label_, lv_color_hex(0xD9EDF4), 0);
    }
    
    // Update bottom bar background color with 50% opacity
    if (bottom_bar_ != nullptr) {
        lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_50, 0);
        lv_obj_set_style_bg_color(bottom_bar_, lv_color_hex(0x0D2433), 0);
    }
#endif
    
    // Update low battery popup
    lv_obj_set_style_bg_color(low_battery_popup_, lvgl_theme->low_battery_color(), 0);

    // No errors occurred. Save theme to settings
    Display::SetTheme(lvgl_theme);
}

void LcdDisplay::SetHideSubtitle(bool hide) {
    DisplayLockGuard lock(this);
    hide_subtitle_ = hide;
    
    // Immediately update UI visibility based on the setting
    if (bottom_bar_ != nullptr) {
        if (hide) {
            lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
        } else {
            // Only show if there is actual content to display
            const char* text = (chat_message_label_ != nullptr) ? lv_label_get_text(chat_message_label_) : nullptr;
            if (text != nullptr && text[0] != '\0') {
                lv_obj_remove_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);
            }
        }
    }
}

void LcdDisplay::ShowClock(const char* time_text, const char* date_text, int battery_percent,
        bool charging, const char* city, const char* condition, float temperature_c, int humidity_percent) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "ShowClock called before SetupUI");
        return;
    }

    DisplayLockGuard lock(this);
    if (clock_panel_ == nullptr) {
        auto theme = static_cast<LvglTheme*>(current_theme_);
        auto text_font = theme->text_font()->font();
        clock_panel_ = lv_obj_create(lv_screen_active());
        lv_obj_set_size(clock_panel_, LV_HOR_RES, LV_VER_RES);
        lv_obj_align(clock_panel_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_radius(clock_panel_, 0, 0);
        lv_obj_set_style_border_width(clock_panel_, 0, 0);
        lv_obj_set_style_pad_all(clock_panel_, 0, 0);
        lv_obj_set_style_bg_color(clock_panel_, lv_color_hex(0x071423), 0);
        lv_obj_set_style_bg_grad_color(clock_panel_, lv_color_hex(0x132C3D), 0);
        lv_obj_set_style_bg_grad_dir(clock_panel_, LV_GRAD_DIR_VER, 0);
        lv_obj_set_style_text_font(clock_panel_, text_font, 0);
        lv_obj_set_scrollbar_mode(clock_panel_, LV_SCROLLBAR_MODE_OFF);

        auto glow = lv_obj_create(clock_panel_);
        lv_obj_set_size(glow, 282, 282);
        lv_obj_align(glow, LV_ALIGN_CENTER, 0, -19);
        lv_obj_set_style_radius(glow, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(glow, lv_color_hex(0x123D50), 0);
        lv_obj_set_style_bg_opa(glow, LV_OPA_30, 0);
        lv_obj_set_style_border_width(glow, 1, 0);
        lv_obj_set_style_border_color(glow, lv_color_hex(0x2F6375), 0);
        lv_obj_remove_flag(glow, LV_OBJ_FLAG_SCROLLABLE);

        clock_city_label_ = lv_label_create(clock_panel_);
        lv_obj_set_width(clock_city_label_, 90);
        lv_label_set_long_mode(clock_city_label_, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(clock_city_label_, lv_color_hex(0xC8E7F1), 0);
        lv_obj_align(clock_city_label_, LV_ALIGN_TOP_LEFT, 88, 34);

        auto battery_card = lv_obj_create(clock_panel_);
        lv_obj_set_size(battery_card, 88, 32);
        lv_obj_align(battery_card, LV_ALIGN_TOP_RIGHT, -88, 28);
        lv_obj_set_style_radius(battery_card, 17, 0);
        lv_obj_set_style_border_width(battery_card, 1, 0);
        lv_obj_set_style_border_color(battery_card, lv_color_hex(0x31566A), 0);
        lv_obj_set_style_bg_color(battery_card, lv_color_hex(0x102A3A), 0);
        lv_obj_set_style_pad_all(battery_card, 0, 0);
        lv_obj_remove_flag(battery_card, LV_OBJ_FLAG_SCROLLABLE);
        clock_battery_label_ = lv_label_create(battery_card);
        lv_obj_center(clock_battery_label_);

        clock_time_label_ = lv_label_create(clock_panel_);
        lv_obj_set_size(clock_time_label_, 180, 52);
        lv_obj_set_style_text_font(clock_time_label_, &lv_font_montserrat_48, 0);
        lv_obj_set_style_text_color(clock_time_label_, lv_color_hex(0xF4FBFF), 0);
        lv_obj_set_style_text_align(clock_time_label_, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_letter_space(clock_time_label_, 3, 0);
        lv_obj_set_style_transform_pivot_x(clock_time_label_, 90, 0);
        lv_obj_set_style_transform_pivot_y(clock_time_label_, 26, 0);
        lv_obj_set_style_transform_scale(clock_time_label_, 330, 0);
        lv_obj_align(clock_time_label_, LV_ALIGN_CENTER, 0, -32);

        clock_date_label_ = lv_label_create(clock_panel_);
        lv_obj_set_style_text_color(clock_date_label_, lv_color_hex(0x8FB2C1), 0);
        lv_obj_set_style_text_letter_space(clock_date_label_, 1, 0);
        lv_obj_align(clock_date_label_, LV_ALIGN_CENTER, 0, 29);

        auto divider = lv_obj_create(clock_panel_);
        lv_obj_set_size(divider, 126, 2);
        lv_obj_align(divider, LV_ALIGN_CENTER, 0, 58);
        lv_obj_set_style_bg_color(divider, lv_color_hex(0x41D9C2), 0);
        lv_obj_set_style_bg_opa(divider, LV_OPA_70, 0);
        lv_obj_set_style_border_width(divider, 0, 0);
        lv_obj_set_style_pad_all(divider, 0, 0);

        auto weather_card = lv_obj_create(clock_panel_);
        lv_obj_set_size(weather_card, 250, 82);
        lv_obj_align(weather_card, LV_ALIGN_BOTTOM_MID, 0, -30);
        lv_obj_set_style_radius(weather_card, 18, 0);
        lv_obj_set_style_border_width(weather_card, 1, 0);
        lv_obj_set_style_border_color(weather_card, lv_color_hex(0x31566A), 0);
        lv_obj_set_style_bg_color(weather_card, lv_color_hex(0x0D2433), 0);
        lv_obj_set_style_bg_opa(weather_card, LV_OPA_80, 0);
        lv_obj_set_style_pad_all(weather_card, 0, 0);
        lv_obj_remove_flag(weather_card, LV_OBJ_FLAG_SCROLLABLE);

        clock_condition_label_ = lv_label_create(weather_card);
        lv_obj_set_width(clock_condition_label_, 116);
        lv_label_set_long_mode(clock_condition_label_, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(clock_condition_label_, lv_color_hex(0xD9EDF4), 0);
        lv_obj_align(clock_condition_label_, LV_ALIGN_TOP_LEFT, 18, 13);

        clock_humidity_label_ = lv_label_create(weather_card);
        lv_obj_set_style_text_color(clock_humidity_label_, lv_color_hex(0x7EA5B5), 0);
        lv_obj_align(clock_humidity_label_, LV_ALIGN_BOTTOM_LEFT, 18, -12);

        clock_temperature_label_ = lv_label_create(weather_card);
        lv_obj_set_style_text_font(clock_temperature_label_, &lv_font_montserrat_48, 0);
        lv_obj_set_style_text_color(clock_temperature_label_, lv_color_hex(0x41D9C2), 0);
        lv_obj_set_style_transform_scale(clock_temperature_label_, 180, 0);
        lv_obj_align(clock_temperature_label_, LV_ALIGN_RIGHT_MID, -22, 0);
    }

    lv_label_set_text(clock_time_label_, time_text != nullptr ? time_text : "--:--");
    lv_label_set_text(clock_date_label_, date_text != nullptr ? date_text : "---- -- --");
    lv_label_set_text(clock_city_label_, city != nullptr ? city : "--");
    lv_label_set_text(clock_condition_label_, condition != nullptr ? condition : "--");
    if (battery_percent >= 0) {
        lv_label_set_text_fmt(clock_battery_label_, charging ? "CHG %d%%" : "BAT %d%%", battery_percent);
        lv_obj_set_style_text_color(clock_battery_label_, charging ? lv_color_hex(0x41E6A1) :
            (battery_percent <= 20 ? lv_color_hex(0xFF6B6B) : lv_color_hex(0xC8E7F1)), 0);
    } else {
        lv_label_set_text(clock_battery_label_, "BAT --");
        lv_obj_set_style_text_color(clock_battery_label_, lv_color_hex(0x8FB2C1), 0);
    }
    lv_label_set_text_fmt(clock_temperature_label_, temperature_c > -999.0f ? "%.0f°" : "--°", temperature_c);
    if (humidity_percent >= 0) {
        lv_label_set_text_fmt(clock_humidity_label_, "湿度 %d%%", humidity_percent);
    } else {
        lv_label_set_text(clock_humidity_label_, "湿度 --");
    }
    lv_obj_move_foreground(clock_panel_);
}

void LcdDisplay::HideClock() {
    DisplayLockGuard lock(this);
    if (clock_panel_ != nullptr) {
        lv_obj_del(clock_panel_);
        clock_panel_ = nullptr;
        clock_time_label_ = nullptr;
        clock_date_label_ = nullptr;
        clock_city_label_ = nullptr;
        clock_battery_label_ = nullptr;
        clock_condition_label_ = nullptr;
        clock_temperature_label_ = nullptr;
        clock_humidity_label_ = nullptr;
        ESP_LOGI(TAG, "Standby clock hidden");
    }
}

void LcdDisplay::ShowServerStatus(float cpu_percent, float disk_percent, float network_rx_kbps, float network_tx_kbps, const char* sampled_at) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "ShowServerStatus called before SetupUI");
        return;
    }

    DisplayLockGuard lock(this);
    if (server_status_panel_ == nullptr) {
        auto theme = static_cast<LvglTheme*>(current_theme_);
        auto text_font = theme->text_font()->font();
        server_status_panel_ = lv_obj_create(lv_screen_active());
        lv_obj_set_size(server_status_panel_, LV_HOR_RES, LV_VER_RES);
        lv_obj_align(server_status_panel_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_radius(server_status_panel_, 0, 0);
        lv_obj_set_style_border_width(server_status_panel_, 0, 0);
        lv_obj_set_style_pad_all(server_status_panel_, 0, 0);
        lv_obj_set_style_bg_color(server_status_panel_, lv_color_hex(0x07111F), 0);
        lv_obj_set_style_text_font(server_status_panel_, text_font, 0);
        lv_obj_set_scrollbar_mode(server_status_panel_, LV_SCROLLBAR_MODE_OFF);

        auto title = lv_label_create(server_status_panel_);
        lv_label_set_text(title, "SERVER STATUS  |  LIVE");
        lv_obj_set_style_text_color(title, lv_color_hex(0xEAF6FF), 0);
        lv_obj_set_style_text_letter_space(title, 1, 0);
        lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 16);

        server_status_time_label_ = lv_label_create(server_status_panel_);
        lv_label_set_text(server_status_time_label_, "UPDATED --:--:--");
        lv_obj_set_style_text_color(server_status_time_label_, lv_color_hex(0x41E6A1), 0);
        lv_obj_align(server_status_time_label_, LV_ALIGN_TOP_MID, 0, 36);

        auto create_chart = [this](int y, const char* name, lv_color_t accent, lv_obj_t** value_label) {
            auto card = lv_obj_create(server_status_panel_);
            lv_obj_set_size(card, 294, 80);
            lv_obj_align(card, LV_ALIGN_TOP_MID, 0, y);
            lv_obj_set_style_radius(card, 12, 0);
            lv_obj_set_style_border_width(card, 1, 0);
            lv_obj_set_style_border_color(card, lv_color_hex(0x20334A), 0);
            lv_obj_set_style_bg_color(card, lv_color_hex(0x101D2E), 0);
            lv_obj_set_style_pad_all(card, 0, 0);
            lv_obj_set_scrollbar_mode(card, LV_SCROLLBAR_MODE_OFF);

            auto label = lv_label_create(card);
            lv_label_set_text(label, name);
            lv_obj_set_style_text_color(label, lv_color_hex(0x8EA4BC), 0);
            lv_obj_align(label, LV_ALIGN_TOP_LEFT, 12, 4);
            *value_label = lv_label_create(card);
            lv_label_set_text(*value_label, "--");
            lv_obj_set_style_text_color(*value_label, accent, 0);
            lv_obj_align(*value_label, LV_ALIGN_TOP_RIGHT, -12, 4);

            auto chart = lv_chart_create(card);
            lv_obj_set_size(chart, 268, 43);
            lv_obj_align(chart, LV_ALIGN_BOTTOM_MID, 0, -3);
            lv_obj_set_style_bg_opa(chart, LV_OPA_TRANSP, 0);
            lv_obj_set_style_border_width(chart, 0, 0);
            lv_obj_set_style_pad_all(chart, 0, 0);
            lv_obj_set_style_line_color(chart, lv_color_hex(0x26384D), LV_PART_MAIN);
            lv_obj_set_style_line_opa(chart, LV_OPA_50, LV_PART_MAIN);
            lv_obj_set_style_line_width(chart, 2, LV_PART_ITEMS);
            lv_obj_set_style_size(chart, 0, 0, LV_PART_INDICATOR);
            lv_chart_set_type(chart, LV_CHART_TYPE_LINE);
            lv_chart_set_update_mode(chart, LV_CHART_UPDATE_MODE_SHIFT);
            lv_chart_set_point_count(chart, 30);
            lv_chart_set_div_line_count(chart, 3, 4);
            return chart;
        };

        server_cpu_chart_ = create_chart(56, "CPU  |  60s", lv_color_hex(0x33D6FF), &server_cpu_value_label_);
        lv_chart_set_axis_range(server_cpu_chart_, LV_CHART_AXIS_PRIMARY_Y, 0, 50);
        server_cpu_series_ = lv_chart_add_series(server_cpu_chart_, lv_color_hex(0x33D6FF), LV_CHART_AXIS_PRIMARY_Y);
        lv_chart_set_all_values(server_cpu_chart_, server_cpu_series_, LV_CHART_POINT_NONE);

        server_disk_chart_ = create_chart(143, "DISK  |  60s", lv_color_hex(0xA98BFF), &server_disk_value_label_);
        lv_chart_set_axis_range(server_disk_chart_, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
        server_disk_series_ = lv_chart_add_series(server_disk_chart_, lv_color_hex(0xA98BFF), LV_CHART_AXIS_PRIMARY_Y);
        lv_chart_set_all_values(server_disk_chart_, server_disk_series_, LV_CHART_POINT_NONE);

        server_network_chart_ = create_chart(230, "NET RX/TX  |  60s", lv_color_hex(0x41E6A1), &server_network_value_label_);
        lv_label_set_recolor(server_network_value_label_, true);
        lv_chart_set_axis_range(server_network_chart_, LV_CHART_AXIS_PRIMARY_Y, 0, 50);
        server_network_rx_series_ = lv_chart_add_series(server_network_chart_, lv_color_hex(0x41E6A1), LV_CHART_AXIS_PRIMARY_Y);
        server_network_tx_series_ = lv_chart_add_series(server_network_chart_, lv_color_hex(0xFFB45E), LV_CHART_AXIS_PRIMARY_Y);
        lv_chart_set_all_values(server_network_chart_, server_network_rx_series_, LV_CHART_POINT_NONE);
        lv_chart_set_all_values(server_network_chart_, server_network_tx_series_, LV_CHART_POINT_NONE);

        auto hint = lv_label_create(server_status_panel_);
        lv_label_set_text(hint, "-60s                         NOW");
        lv_obj_set_style_text_color(hint, lv_color_hex(0x607891), 0);
        lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -17);
        lv_obj_move_foreground(server_status_panel_);
    }

    lv_label_set_text_fmt(server_status_time_label_, "UPDATED %s", sampled_at != nullptr ? sampled_at : "--:--:--");
    lv_label_set_text_fmt(server_cpu_value_label_, cpu_percent >= 0.0f ? "%.1f%%" : "--", cpu_percent);
    lv_label_set_text_fmt(server_disk_value_label_, disk_percent >= 0.0f ? "%.1f%%" : "--", disk_percent);
    if (network_rx_kbps >= 0.0f && network_tx_kbps >= 0.0f) {
        lv_label_set_text_fmt(server_network_value_label_,
            "#41E6A1 R %.1f#  #FFB45E T %.1f#", network_rx_kbps, network_tx_kbps);
    } else {
        lv_label_set_text(server_network_value_label_, "#41E6A1 R --#  #FFB45E T --#");
    }

    PushChartSample(server_cpu_history_, cpu_percent);
    PushChartSample(server_network_rx_history_, network_rx_kbps);
    PushChartSample(server_network_tx_history_, network_tx_kbps);
    server_cpu_scale_percent_ = NiceChartUpper(RecentPeak(server_cpu_history_), 5.0f, 100.0f);
    server_network_scale_kbps_ = NiceChartUpper(
        RecentPeak(server_network_rx_history_, &server_network_tx_history_), 5.0f, 1000000.0f);

    lv_chart_set_axis_range(server_cpu_chart_, LV_CHART_AXIS_PRIMARY_Y, 0,
        static_cast<int32_t>(std::lround(server_cpu_scale_percent_ * 10.0f)));
    lv_chart_set_next_value(server_cpu_chart_, server_cpu_series_,
        cpu_percent >= 0.0f ? std::lround(cpu_percent * 10.0f) : LV_CHART_POINT_NONE);
    lv_chart_set_next_value(server_disk_chart_, server_disk_series_, disk_percent >= 0.0f ? std::lround(disk_percent) : LV_CHART_POINT_NONE);
    lv_chart_set_axis_range(server_network_chart_, LV_CHART_AXIS_PRIMARY_Y, 0,
        static_cast<int32_t>(std::lround(server_network_scale_kbps_ * 10.0f)));
    lv_chart_set_next_value(server_network_chart_, server_network_rx_series_,
        network_rx_kbps >= 0.0f ? std::lround(network_rx_kbps * 10.0f) : LV_CHART_POINT_NONE);
    lv_chart_set_next_value(server_network_chart_, server_network_tx_series_,
        network_tx_kbps >= 0.0f ? std::lround(network_tx_kbps * 10.0f) : LV_CHART_POINT_NONE);
}

void LcdDisplay::HideServerStatus() {
    DisplayLockGuard lock(this);
    if (server_status_panel_ != nullptr) {
        lv_obj_del(server_status_panel_);
        server_status_panel_ = nullptr;
        server_status_time_label_ = nullptr;
        server_cpu_value_label_ = nullptr;
        server_disk_value_label_ = nullptr;
        server_network_value_label_ = nullptr;
        server_cpu_chart_ = nullptr;
        server_disk_chart_ = nullptr;
        server_network_chart_ = nullptr;
        server_cpu_series_ = nullptr;
        server_disk_series_ = nullptr;
        server_network_rx_series_ = nullptr;
        server_network_tx_series_ = nullptr;
        server_cpu_history_.fill(0.0f);
        server_network_rx_history_.fill(0.0f);
        server_network_tx_history_.fill(0.0f);
        server_cpu_scale_percent_ = 5.0f;
        server_network_scale_kbps_ = 5.0f;
        ESP_LOGI(TAG, "Server status dashboard hidden");
    }
}

void LcdDisplay::ShowPomodoro(const char* state, int remaining_seconds, int total_seconds, const char* label) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "ShowPomodoro called before SetupUI");
        return;
    }

    DisplayLockGuard lock(this);
    if (pomodoro_panel_ == nullptr) {
        auto theme = static_cast<LvglTheme*>(current_theme_);
        auto text_font = theme->text_font()->font();
        pomodoro_panel_ = lv_obj_create(lv_screen_active());
        lv_obj_set_size(pomodoro_panel_, LV_HOR_RES, LV_VER_RES);
        lv_obj_align(pomodoro_panel_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_radius(pomodoro_panel_, 0, 0);
        lv_obj_set_style_border_width(pomodoro_panel_, 0, 0);
        lv_obj_set_style_pad_all(pomodoro_panel_, 0, 0);
        lv_obj_set_style_bg_color(pomodoro_panel_, lv_color_hex(0x100B18), 0);
        lv_obj_set_style_text_font(pomodoro_panel_, text_font, 0);
        lv_obj_set_scrollbar_mode(pomodoro_panel_, LV_SCROLLBAR_MODE_OFF);

        auto title = lv_label_create(pomodoro_panel_);
        lv_label_set_text(title, "FOCUS TIMER");
        lv_obj_set_style_text_color(title, lv_color_hex(0xFFB45E), 0);
        lv_obj_set_style_text_letter_space(title, 2, 0);
        lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 18);

        pomodoro_name_label_ = lv_label_create(pomodoro_panel_);
        lv_obj_set_width(pomodoro_name_label_, 270);
        lv_obj_set_style_text_align(pomodoro_name_label_, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(pomodoro_name_label_, lv_color_hex(0xC9BBD8), 0);
        lv_obj_align(pomodoro_name_label_, LV_ALIGN_TOP_MID, 0, 48);

        pomodoro_arc_ = lv_arc_create(pomodoro_panel_);
        lv_obj_set_size(pomodoro_arc_, 220, 220);
        lv_obj_align(pomodoro_arc_, LV_ALIGN_CENTER, 0, 12);
        lv_obj_remove_flag(pomodoro_arc_, LV_OBJ_FLAG_CLICKABLE);
        lv_arc_set_rotation(pomodoro_arc_, 270);
        lv_arc_set_bg_angles(pomodoro_arc_, 0, 360);
        lv_arc_set_range(pomodoro_arc_, 0, 1000);
        lv_obj_set_style_arc_width(pomodoro_arc_, 15, LV_PART_MAIN);
        lv_obj_set_style_arc_color(pomodoro_arc_, lv_color_hex(0x392A47), LV_PART_MAIN);
        lv_obj_set_style_arc_width(pomodoro_arc_, 15, LV_PART_INDICATOR);
        lv_obj_set_style_arc_color(pomodoro_arc_, lv_color_hex(0xFF6B6B), LV_PART_INDICATOR);
        lv_obj_set_style_bg_opa(pomodoro_arc_, LV_OPA_TRANSP, LV_PART_KNOB);

        pomodoro_time_label_ = lv_label_create(pomodoro_panel_);
        lv_obj_set_style_text_font(pomodoro_time_label_, &lv_font_montserrat_48, 0);
        lv_obj_set_style_text_color(pomodoro_time_label_, lv_color_hex(0xFFF7EE), 0);
        lv_obj_set_style_text_letter_space(pomodoro_time_label_, 1, 0);
        lv_obj_align(pomodoro_time_label_, LV_ALIGN_CENTER, 0, -2);

        pomodoro_state_label_ = lv_label_create(pomodoro_panel_);
        lv_obj_align(pomodoro_state_label_, LV_ALIGN_CENTER, 0, 48);

        pomodoro_hint_label_ = lv_label_create(pomodoro_panel_);
        lv_label_set_text(pomodoro_hint_label_, "唤醒后可暂停、继续或取消");
        lv_obj_set_style_text_color(pomodoro_hint_label_, lv_color_hex(0x8F819C), 0);
        lv_obj_align(pomodoro_hint_label_, LV_ALIGN_BOTTOM_MID, 0, -18);
    }

    remaining_seconds = std::max(0, remaining_seconds);
    total_seconds = std::max(1, total_seconds);
    const int minutes = remaining_seconds / 60;
    const int seconds = remaining_seconds % 60;
    lv_label_set_text_fmt(pomodoro_time_label_, "%02d:%02d", minutes, seconds);
    lv_label_set_text(pomodoro_name_label_, label != nullptr ? label : "番茄钟");
    const int elapsed = std::max(0, total_seconds - remaining_seconds);
    lv_arc_set_value(pomodoro_arc_, std::min(1000, elapsed * 1000 / total_seconds));

    if (strcmp(state, "paused") == 0) {
        lv_label_set_text(pomodoro_state_label_, "已暂停");
        lv_obj_set_style_text_color(pomodoro_state_label_, lv_color_hex(0xFFD166), 0);
    } else if (strcmp(state, "finished") == 0) {
        lv_label_set_text(pomodoro_state_label_, "专注完成");
        lv_obj_set_style_text_color(pomodoro_state_label_, lv_color_hex(0x41E6A1), 0);
        lv_arc_set_value(pomodoro_arc_, 1000);
    } else {
        lv_label_set_text(pomodoro_state_label_, "专注中");
        lv_obj_set_style_text_color(pomodoro_state_label_, lv_color_hex(0xFF8C7A), 0);
    }
    lv_obj_move_foreground(pomodoro_panel_);
}

void LcdDisplay::HidePomodoro() {
    DisplayLockGuard lock(this);
    if (pomodoro_panel_ != nullptr) {
        lv_obj_del(pomodoro_panel_);
        pomodoro_panel_ = nullptr;
        pomodoro_arc_ = nullptr;
        pomodoro_time_label_ = nullptr;
        pomodoro_state_label_ = nullptr;
        pomodoro_name_label_ = nullptr;
        pomodoro_hint_label_ = nullptr;
        ESP_LOGI(TAG, "Pomodoro page hidden");
    }
}

void LcdDisplay::ShowReminder(const char* kind, const char* title, const char* time_text) {
    if (!setup_ui_called_) {
        ESP_LOGW(TAG, "ShowReminder called before SetupUI");
        return;
    }
    DisplayLockGuard lock(this);
    if (reminder_panel_ == nullptr) {
        auto theme = static_cast<LvglTheme*>(current_theme_);
        auto text_font = theme->text_font()->font();
        reminder_panel_ = lv_obj_create(lv_screen_active());
        lv_obj_set_size(reminder_panel_, LV_HOR_RES, LV_VER_RES);
        lv_obj_align(reminder_panel_, LV_ALIGN_CENTER, 0, 0);
        lv_obj_set_style_radius(reminder_panel_, 0, 0);
        lv_obj_set_style_border_width(reminder_panel_, 0, 0);
        lv_obj_set_style_pad_all(reminder_panel_, 0, 0);
        lv_obj_set_style_bg_color(reminder_panel_, lv_color_hex(0x081322), 0);
        lv_obj_set_style_text_font(reminder_panel_, text_font, 0);
        lv_obj_set_scrollbar_mode(reminder_panel_, LV_SCROLLBAR_MODE_OFF);

        reminder_kind_label_ = lv_label_create(reminder_panel_);
        lv_obj_set_style_text_color(reminder_kind_label_, lv_color_hex(0xFFB45E), 0);
        lv_obj_set_style_text_letter_space(reminder_kind_label_, 2, 0);
        lv_obj_align(reminder_kind_label_, LV_ALIGN_TOP_MID, 0, 30);

        reminder_time_label_ = lv_label_create(reminder_panel_);
        lv_obj_set_style_text_font(reminder_time_label_, &lv_font_montserrat_48, 0);
        lv_obj_set_style_text_color(reminder_time_label_, lv_color_hex(0xFFFFFF), 0);
        lv_obj_align(reminder_time_label_, LV_ALIGN_CENTER, 0, -42);

        reminder_title_label_ = lv_label_create(reminder_panel_);
        lv_obj_set_width(reminder_title_label_, 250);
        lv_label_set_long_mode(reminder_title_label_, LV_LABEL_LONG_WRAP);
        lv_obj_set_style_text_align(reminder_title_label_, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(reminder_title_label_, lv_color_hex(0xCFE7FF), 0);
        lv_obj_align(reminder_title_label_, LV_ALIGN_CENTER, 0, 35);

        auto hint = lv_label_create(reminder_panel_);
        lv_label_set_text(hint, "说唤醒词即可停止");
        lv_obj_set_style_text_color(hint, lv_color_hex(0x8195AA), 0);
        lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -28);
    }
    const char* kind_text = "REMINDER";
    if (strcmp(kind, "alarm") == 0) {
        kind_text = "ALARM";
    } else if (strcmp(kind, "todo") == 0) {
        kind_text = "TODO";
    }
    lv_label_set_text(reminder_kind_label_, kind_text);
    lv_label_set_text(reminder_title_label_, title != nullptr ? title : "提醒时间到了");
    lv_label_set_text(reminder_time_label_, time_text != nullptr ? time_text : "--:--");
    lv_obj_move_foreground(reminder_panel_);
}

void LcdDisplay::HideReminder() {
    DisplayLockGuard lock(this);
    if (reminder_panel_ != nullptr) {
        lv_obj_del(reminder_panel_);
        reminder_panel_ = nullptr;
        reminder_kind_label_ = nullptr;
        reminder_title_label_ = nullptr;
        reminder_time_label_ = nullptr;
        ESP_LOGI(TAG, "Reminder page hidden");
    }
}
