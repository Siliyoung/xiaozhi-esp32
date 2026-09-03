#include "application.h"
#include "board.h"
#include "display.h"
#include "system_info.h"
#include "audio_codec.h"
#include "mqtt_protocol.h"
#include "websocket_protocol.h"
#include "assets/lang_config.h"
#include "mcp_server.h"
#include "assets.h"
#include "settings.h"

#include <cstring>
#include <ctime>
#include <esp_log.h>
#include <esp_heap_caps.h>
#include <esp_wifi.h>
#include <cJSON.h>
#include <driver/gpio.h>
#include <arpa/inet.h>
#include <font_awesome.h>

#define TAG "Application"

namespace {
constexpr int64_t kClockWeatherRefreshIntervalUs = 30LL * 60LL * 1000000LL;
}


Application::Application() {
    event_group_ = xEventGroupCreate();

#if CONFIG_USE_DEVICE_AEC && CONFIG_USE_SERVER_AEC
#error "CONFIG_USE_DEVICE_AEC and CONFIG_USE_SERVER_AEC cannot be enabled at the same time"
#elif CONFIG_USE_DEVICE_AEC
    aec_mode_ = kAecOnDeviceSide;
#elif CONFIG_USE_SERVER_AEC
    aec_mode_ = kAecOnServerSide;
#else
    aec_mode_ = kAecOff;
#endif

    esp_timer_create_args_t clock_timer_args = {
        .callback = [](void* arg) {
            Application* app = (Application*)arg;
            xEventGroupSetBits(app->event_group_, MAIN_EVENT_CLOCK_TICK);
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "clock_timer",
        .skip_unhandled_events = true
    };
    esp_timer_create(&clock_timer_args, &clock_timer_handle_);
}

Application::~Application() {
    if (clock_timer_handle_ != nullptr) {
        esp_timer_stop(clock_timer_handle_);
        esp_timer_delete(clock_timer_handle_);
    }
    vEventGroupDelete(event_group_);
}

bool Application::SetDeviceState(DeviceState state) {
    return state_machine_.TransitionTo(state);
}

void Application::Initialize() {
    auto& board = Board::GetInstance();
    SetDeviceState(kDeviceStateStarting);

    // Setup the display
    auto display = board.GetDisplay();
    display->SetupUI();
    // Print board name/version info
    display->SetChatMessage("system", SystemInfo::GetUserAgent().c_str());

    // Setup the audio service
    auto codec = board.GetAudioCodec();
    audio_service_.Initialize(codec);
    audio_service_.Start();

    AudioServiceCallbacks callbacks;
    callbacks.on_send_queue_available = [this]() {
        xEventGroupSetBits(event_group_, MAIN_EVENT_SEND_AUDIO);
    };
    callbacks.on_wake_word_detected = [this](const std::string& wake_word) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_WAKE_WORD_DETECTED);
    };
    callbacks.on_vad_change = [this](bool speaking) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_VAD_CHANGE);
    };
    audio_service_.SetCallbacks(callbacks);

    // Add state change listeners
    state_machine_.AddStateChangeListener([this](DeviceState old_state, DeviceState new_state) {
        xEventGroupSetBits(event_group_, MAIN_EVENT_STATE_CHANGED);
    });

    // Start the clock timer to update the status bar
    esp_timer_start_periodic(clock_timer_handle_, 1000000);

    // Add MCP common tools (only once during initialization)
    auto& mcp_server = McpServer::GetInstance();
    mcp_server.AddCommonTools();
    mcp_server.AddUserOnlyTools();

    // Set network event callback for UI updates and network state handling
    board.SetNetworkEventCallback([this](NetworkEvent event, const std::string& data) {
        auto display = Board::GetInstance().GetDisplay();
        
        switch (event) {
            case NetworkEvent::Scanning:
                display->ShowNotification(Lang::Strings::SCANNING_WIFI, 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::Connecting: {
                if (data.empty()) {
                    // Cellular network - registering without carrier info yet
                    display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                } else {
                    // WiFi or cellular with carrier info
                    std::string msg = Lang::Strings::CONNECT_TO;
                    msg += data;
                    msg += "...";
                    display->ShowNotification(msg.c_str(), 30000);
                }
                break;
            }
            case NetworkEvent::Connected: {
                std::string msg = Lang::Strings::CONNECTED_TO;
                msg += data;
                display->ShowNotification(msg.c_str(), 30000);
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_CONNECTED);
                break;
            }
            case NetworkEvent::Disconnected:
                xEventGroupSetBits(event_group_, MAIN_EVENT_NETWORK_DISCONNECTED);
                break;
            case NetworkEvent::WifiConfigModeEnter:
                // WiFi config mode enter is handled by WifiBoard internally
                break;
            case NetworkEvent::WifiConfigModeExit:
                // WiFi config mode exit is handled by WifiBoard internally
                break;
            // Cellular modem specific events
            case NetworkEvent::ModemDetecting:
                display->SetStatus(Lang::Strings::DETECTING_MODULE);
                break;
            case NetworkEvent::ModemErrorNoSim:
                Alert(Lang::Strings::ERROR, Lang::Strings::PIN_ERROR, "triangle_exclamation", Lang::Sounds::OGG_ERR_PIN);
                break;
            case NetworkEvent::ModemErrorRegDenied:
                Alert(Lang::Strings::ERROR, Lang::Strings::REG_ERROR, "triangle_exclamation", Lang::Sounds::OGG_ERR_REG);
                break;
            case NetworkEvent::ModemErrorInitFailed:
                Alert(Lang::Strings::ERROR, Lang::Strings::MODEM_INIT_ERROR, "triangle_exclamation", Lang::Sounds::OGG_EXCLAMATION);
                break;
            case NetworkEvent::ModemErrorTimeout:
                display->SetStatus(Lang::Strings::REGISTERING_NETWORK);
                break;
        }
    });

    // Start network asynchronously
    board.StartNetwork();

    // Update the status bar immediately to show the network state
    display->UpdateStatusBar(true);
}

void Application::Run() {
    // Set the priority of the main task to 10
    vTaskPrioritySet(nullptr, 10);

    const EventBits_t ALL_EVENTS = 
        MAIN_EVENT_SCHEDULE |
        MAIN_EVENT_SEND_AUDIO |
        MAIN_EVENT_WAKE_WORD_DETECTED |
        MAIN_EVENT_VAD_CHANGE |
        MAIN_EVENT_CLOCK_TICK |
        MAIN_EVENT_ERROR |
        MAIN_EVENT_NETWORK_CONNECTED |
        MAIN_EVENT_NETWORK_DISCONNECTED |
        MAIN_EVENT_TOGGLE_CHAT |
        MAIN_EVENT_START_LISTENING |
        MAIN_EVENT_STOP_LISTENING |
        MAIN_EVENT_ACTIVATION_DONE |
        MAIN_EVENT_STATE_CHANGED;

    while (true) {
        auto bits = xEventGroupWaitBits(event_group_, ALL_EVENTS, pdTRUE, pdFALSE, portMAX_DELAY);

        if (bits & MAIN_EVENT_ERROR) {
            SetDeviceState(kDeviceStateIdle);
            Alert(Lang::Strings::ERROR, last_error_message_.c_str(), "circle_xmark", Lang::Sounds::OGG_EXCLAMATION);
        }

        if (bits & MAIN_EVENT_NETWORK_CONNECTED) {
            HandleNetworkConnectedEvent();
        }

        if (bits & MAIN_EVENT_NETWORK_DISCONNECTED) {
            HandleNetworkDisconnectedEvent();
        }

        if (bits & MAIN_EVENT_ACTIVATION_DONE) {
            HandleActivationDoneEvent();
        }

        if (bits & MAIN_EVENT_STATE_CHANGED) {
            HandleStateChangedEvent();
        }

        if (bits & MAIN_EVENT_TOGGLE_CHAT) {
            HandleToggleChatEvent();
        }

        if (bits & MAIN_EVENT_START_LISTENING) {
            HandleStartListeningEvent();
        }

        if (bits & MAIN_EVENT_STOP_LISTENING) {
            HandleStopListeningEvent();
        }

        if (bits & MAIN_EVENT_SEND_AUDIO) {
            while (auto packet = audio_service_.PopPacketFromSendQueue()) {
                if (protocol_ && !protocol_->SendAudio(std::move(packet))) {
                    break;
                }
            }
        }

        if (bits & MAIN_EVENT_WAKE_WORD_DETECTED) {
            HandleWakeWordDetectedEvent();
        }

        if (bits & MAIN_EVENT_VAD_CHANGE) {
            if (GetDeviceState() == kDeviceStateListening) {
                auto led = Board::GetInstance().GetLed();
                led->OnStateChanged();
            }
        }

        if (bits & MAIN_EVENT_SCHEDULE) {
            std::unique_lock<std::mutex> lock(mutex_);
            auto tasks = std::move(main_tasks_);
            lock.unlock();
            for (auto& task : tasks) {
                task();
            }
        }

        if (bits & MAIN_EVENT_CLOCK_TICK) {
            clock_ticks_++;
            auto display = Board::GetInstance().GetDisplay();
            display->UpdateStatusBar();
            UpdatePomodoroTimer();
            UpdateReminderSchedule();
            UpdateClockFace();
        
            const int64_t now_us = esp_timer_get_time();
            if (GetDeviceState() == kDeviceStateIdle &&
                    now_us - last_clock_weather_refresh_us_ >= kClockWeatherRefreshIntervalUs) {
                RefreshClockWeather();
            }

            // Print debug info every 10 seconds
            if (clock_ticks_ % 10 == 0) {
                SystemInfo::PrintHeapStats();
            }
        }
    }
}

void Application::HandleNetworkConnectedEvent() {
    ESP_LOGI(TAG, "Network connected");
    auto state = GetDeviceState();

    if (state == kDeviceStateStarting || state == kDeviceStateWifiConfiguring) {
        // Network is ready, start activation
        SetDeviceState(kDeviceStateActivating);
        if (activation_task_handle_ != nullptr) {
            ESP_LOGW(TAG, "Activation task already running");
            return;
        }

        xTaskCreate([](void* arg) {
            Application* app = static_cast<Application*>(arg);
            app->ActivationTask();
            app->activation_task_handle_ = nullptr;
            vTaskDelete(NULL);
        }, "activation", 4096 * 2, this, 2, &activation_task_handle_);
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleNetworkDisconnectedEvent() {
    // Close current conversation when network disconnected
    auto state = GetDeviceState();
    if (state == kDeviceStateConnecting || state == kDeviceStateListening || state == kDeviceStateSpeaking) {
        ESP_LOGI(TAG, "Closing audio channel due to network disconnection");
        protocol_->CloseAudioChannel();
    }

    // Update the status bar immediately to show the network state
    auto display = Board::GetInstance().GetDisplay();
    display->UpdateStatusBar(true);
}

void Application::HandleActivationDoneEvent() {
    ESP_LOGI(TAG, "Activation done");

    SystemInfo::PrintHeapStats();
    has_server_time_ = ota_->HasServerTime();
    if (ota_->HasClockContext()) {
        clock_city_ = ota_->GetClockCity();
        clock_condition_ = ota_->GetClockCondition();
        clock_temperature_c_ = ota_->GetClockTemperatureC();
        clock_humidity_percent_ = ota_->GetClockHumidityPercent();
    }
    last_clock_weather_refresh_us_ = esp_timer_get_time();
    SetDeviceState(kDeviceStateIdle);

    auto display = Board::GetInstance().GetDisplay();
    std::string message = std::string(Lang::Strings::VERSION) + ota_->GetCurrentVersion();
    display->ShowNotification(message.c_str());
    display->SetChatMessage("system", "");

    // Release OTA object after activation is complete
    ota_.reset();
    auto& board = Board::GetInstance();
    board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);

    Schedule([this]() {
        // Play the success sound to indicate the device is ready
        audio_service_.PlaySound(Lang::Sounds::OGG_SUCCESS);
    });
}

void Application::ActivationTask() {
    // Create OTA object for activation process
    ota_ = std::make_unique<Ota>();

    // Check for new assets version
    CheckAssetsVersion();

    // Check for new firmware version
    CheckNewVersion();

    // Initialize the protocol
    InitializeProtocol();

    // Signal completion to main loop
    xEventGroupSetBits(event_group_, MAIN_EVENT_ACTIVATION_DONE);
}

void Application::CheckAssetsVersion() {
    // Only allow CheckAssetsVersion to be called once
    if (assets_version_checked_) {
        return;
    }
    assets_version_checked_ = true;

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto& assets = Assets::GetInstance();

    if (!assets.partition_valid()) {
        ESP_LOGW(TAG, "Assets partition is disabled for board %s", BOARD_NAME);
        return;
    }
    
    Settings settings("assets", true);
    // Check if there is a new assets need to be downloaded
    std::string download_url = settings.GetString("download_url");

    if (!download_url.empty()) {
        settings.EraseKey("download_url");

        char message[256];
        snprintf(message, sizeof(message), Lang::Strings::FOUND_NEW_ASSETS, download_url.c_str());
        Alert(Lang::Strings::LOADING_ASSETS, message, "cloud_arrow_down", Lang::Sounds::OGG_UPGRADE);
        
        // Wait for the audio service to be idle for 3 seconds
        vTaskDelay(pdMS_TO_TICKS(3000));
        SetDeviceState(kDeviceStateUpgrading);
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        display->SetChatMessage("system", Lang::Strings::PLEASE_WAIT);

        bool success = assets.Download(download_url, [this, display](int progress, size_t speed) -> void {
            char buffer[32];
            snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
            Schedule([display, message = std::string(buffer)]() {
                display->SetChatMessage("system", message.c_str());
            });
        });

        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        vTaskDelay(pdMS_TO_TICKS(1000));

        if (!success) {
            Alert(Lang::Strings::ERROR, Lang::Strings::DOWNLOAD_ASSETS_FAILED, "circle_xmark", Lang::Sounds::OGG_EXCLAMATION);
            vTaskDelay(pdMS_TO_TICKS(2000));
            SetDeviceState(kDeviceStateActivating);
            return;
        }
    }

    // Apply assets
    assets.Apply();
    display->SetChatMessage("system", "");
    display->SetEmotion("microchip_ai");
}

void Application::CheckNewVersion() {
    const int MAX_RETRY = 10;
    int retry_count = 0;
    int retry_delay = 10; // Initial retry delay in seconds

    auto& board = Board::GetInstance();
    while (true) {
        auto display = board.GetDisplay();
        display->SetStatus(Lang::Strings::CHECKING_NEW_VERSION);

        esp_err_t err = ota_->CheckVersion();
        if (err != ESP_OK) {
            retry_count++;
            if (retry_count >= MAX_RETRY) {
                ESP_LOGE(TAG, "Too many retries, exit version check");
                return;
            }

            char error_message[128];
            snprintf(error_message, sizeof(error_message), "code=%d, url=%s", err, ota_->GetCheckVersionUrl().c_str());
            char buffer[256];
            snprintf(buffer, sizeof(buffer), Lang::Strings::CHECK_NEW_VERSION_FAILED, retry_delay, error_message);
            Alert(Lang::Strings::ERROR, buffer, "cloud_slash", Lang::Sounds::OGG_EXCLAMATION);

            ESP_LOGW(TAG, "Check new version failed, retry in %d seconds (%d/%d)", retry_delay, retry_count, MAX_RETRY);
            for (int i = 0; i < retry_delay; i++) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                if (GetDeviceState() == kDeviceStateIdle) {
                    break;
                }
            }
            retry_delay *= 2; // Double the retry delay
            continue;
        }
        retry_count = 0;
        retry_delay = 10; // Reset retry delay

        if (ota_->HasNewVersion()) {
            if (UpgradeFirmware(ota_->GetFirmwareUrl(), ota_->GetFirmwareVersion())) {
                return; // This line will never be reached after reboot
            }
            // If upgrade failed, continue to normal operation
        }

        // No new version, mark the current version as valid
        ota_->MarkCurrentVersionValid();
        if (!ota_->HasActivationCode() && !ota_->HasActivationChallenge()) {
            // Exit the loop if done checking new version
            break;
        }

        display->SetStatus(Lang::Strings::ACTIVATION);
        // Activation code is shown to the user and waiting for the user to input
        if (ota_->HasActivationCode()) {
            ShowActivationCode(ota_->GetActivationCode(), ota_->GetActivationMessage());
        }

        // This will block the loop until the activation is done or timeout
        for (int i = 0; i < 10; ++i) {
            ESP_LOGI(TAG, "Activating... %d/%d", i + 1, 10);
            esp_err_t err = ota_->Activate();
            if (err == ESP_OK) {
                break;
            } else if (err == ESP_ERR_TIMEOUT) {
                vTaskDelay(pdMS_TO_TICKS(3000));
            } else {
                vTaskDelay(pdMS_TO_TICKS(10000));
            }
            if (GetDeviceState() == kDeviceStateIdle) {
                break;
            }
        }
    }
}

void Application::InitializeProtocol() {
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto codec = board.GetAudioCodec();

    display->SetStatus(Lang::Strings::LOADING_PROTOCOL);

    if (ota_->HasMqttConfig()) {
        protocol_ = std::make_unique<MqttProtocol>();
    } else if (ota_->HasWebsocketConfig()) {
        protocol_ = std::make_unique<WebsocketProtocol>();
    } else {
        ESP_LOGW(TAG, "No protocol specified in the OTA config, using MQTT");
        protocol_ = std::make_unique<MqttProtocol>();
    }

    protocol_->OnConnected([this]() {
        DismissAlert();
    });

    protocol_->OnNetworkError([this](const std::string& message) {
        last_error_message_ = message;
        xEventGroupSetBits(event_group_, MAIN_EVENT_ERROR);
    });
    
    protocol_->OnIncomingAudio([this](std::unique_ptr<AudioStreamPacket> packet) {
        if (GetDeviceState() == kDeviceStateSpeaking) {
            audio_service_.PushPacketToDecodeQueue(std::move(packet));
        }
    });
    
    protocol_->OnAudioChannelOpened([this, codec, &board]() {
        board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
        if (protocol_->server_sample_rate() != codec->output_sample_rate()) {
            ESP_LOGW(TAG, "Server sample rate %d does not match device output sample rate %d, resampling may cause distortion",
                protocol_->server_sample_rate(), codec->output_sample_rate());
        }
    });
    
    protocol_->OnAudioChannelClosed([this, &board]() {
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER);
        Schedule([this]() {
            auto display = Board::GetInstance().GetDisplay();
            display->SetChatMessage("system", "");
            if (pomodoro_timer_.state() != PomodoroState::kIdle && !server_dashboard_active_) {
                pomodoro_page_visible_ = true;
                ShowPomodoroPage();
            }
            SetDeviceState(kDeviceStateIdle);
        });
    });
    
    protocol_->OnIncomingJson([this, display](const cJSON* root) {
        // Parse JSON data
        auto type = cJSON_GetObjectItem(root, "type");
        if (strcmp(type->valuestring, "tts") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            if (strcmp(state->valuestring, "start") == 0) {
                Schedule([this]() {
                    aborted_ = false;
                    SetDeviceState(kDeviceStateSpeaking);
                });
            } else if (strcmp(state->valuestring, "stop") == 0) {
                const int64_t received_us = esp_timer_get_time();
                Schedule([this, received_us]() {
                    ReportBargeInStopReceived(received_us);
                    if (GetDeviceState() == kDeviceStateSpeaking) {
                        if (listening_mode_ == kListeningModeManualStop) {
                            SetDeviceState(kDeviceStateIdle);
                        } else {
                            SetDeviceState(kDeviceStateListening);
                        }
                        if (pomodoro_timer_.state() != PomodoroState::kIdle && !server_dashboard_active_) {
                            pomodoro_page_visible_ = true;
                            ShowPomodoroPage();
                        }
                    }
                });
            } else if (strcmp(state->valuestring, "sentence_start") == 0) {
                auto text = cJSON_GetObjectItem(root, "text");
                if (cJSON_IsString(text)) {
                    ESP_LOGI(TAG, "<< %s", text->valuestring);
                    Schedule([display, message = std::string(text->valuestring)]() {
                        display->SetChatMessage("assistant", message.c_str());
                    });
                }
            }
        } else if (strcmp(type->valuestring, "stt") == 0) {
            auto text = cJSON_GetObjectItem(root, "text");
            if (cJSON_IsString(text)) {
                ESP_LOGI(TAG, ">> %s", text->valuestring);
                Schedule([display, message = std::string(text->valuestring)]() {
                    display->SetChatMessage("user", message.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "clock_context") == 0) {
            auto data = cJSON_GetObjectItem(root, "data");
            if (!cJSON_IsObject(data)) {
                ESP_LOGW(TAG, "Clock context message is missing data");
                return;
            }
            auto city = cJSON_GetObjectItem(data, "city");
            auto condition = cJSON_GetObjectItem(data, "condition");
            auto temperature = cJSON_GetObjectItem(data, "temperature_c");
            auto humidity = cJSON_GetObjectItem(data, "humidity_percent");
            if (!cJSON_IsString(city) || !cJSON_IsString(condition)) {
                ESP_LOGW(TAG, "Clock context requires city and condition");
                return;
            }
            const float temperature_c = cJSON_IsNumber(temperature)
                ? static_cast<float>(temperature->valuedouble) : -1000.0f;
            const int humidity_percent = cJSON_IsNumber(humidity) ? humidity->valueint : -1;
            Schedule([this, city_text = std::string(city->valuestring),
                    condition_text = std::string(condition->valuestring),
                    temperature_c, humidity_percent]() {
                UpdateClockContext(city_text, condition_text, temperature_c, humidity_percent);
            });
        } else if (strcmp(type->valuestring, "pomodoro") == 0) {
            auto action = cJSON_GetObjectItem(root, "action");
            auto duration = cJSON_GetObjectItem(root, "duration_seconds");
            auto label = cJSON_GetObjectItem(root, "label");
            if (!cJSON_IsString(action)) {
                ESP_LOGW(TAG, "Pomodoro command requires an action");
                return;
            }
            int duration_seconds = cJSON_IsNumber(duration) ? duration->valueint : 0;
            std::string timer_label = cJSON_IsString(label) ? label->valuestring : "番茄钟";
            Schedule([this, action_name = std::string(action->valuestring), duration_seconds, timer_label]() {
                HandlePomodoroCommand(action_name, duration_seconds, timer_label);
            });
        } else if (strcmp(type->valuestring, "reminder") == 0) {
            auto action = cJSON_GetObjectItem(root, "action");
            auto id = cJSON_GetObjectItem(root, "id");
            auto trigger = cJSON_GetObjectItem(root, "trigger_at_epoch");
            auto title = cJSON_GetObjectItem(root, "title");
            auto kind = cJSON_GetObjectItem(root, "kind");
            if (!cJSON_IsString(action) || !cJSON_IsNumber(id)) {
                ESP_LOGW(TAG, "Reminder command requires action and id");
                return;
            }
            const int reminder_id = id->valueint;
            const int64_t trigger_epoch = cJSON_IsNumber(trigger)
                ? static_cast<int64_t>(trigger->valuedouble) : 0;
            const std::string reminder_title = cJSON_IsString(title)
                ? title->valuestring : "提醒时间到了";
            const std::string reminder_kind = cJSON_IsString(kind)
                ? kind->valuestring : "reminder";
            Schedule([this, action_name = std::string(action->valuestring), reminder_id,
                    trigger_epoch, reminder_title, reminder_kind]() {
                HandleReminderCommand(action_name, reminder_id, trigger_epoch,
                    reminder_title, reminder_kind);
            });
        } else if (strcmp(type->valuestring, "server_status") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            if (cJSON_IsString(state) && strcmp(state->valuestring, "stop") == 0) {
                Schedule([this, display]() {
                    server_dashboard_active_ = false;
                    display->HideServerStatus();
                });
                return;
            }
            auto data = cJSON_GetObjectItem(root, "data");
            if (cJSON_IsObject(data)) {
                auto number_or_missing = [data](const char* name) {
                    auto item = cJSON_GetObjectItem(data, name);
                    return cJSON_IsNumber(item) ? static_cast<float>(item->valuedouble) : -1.0f;
                };
                auto sampled_at_item = cJSON_GetObjectItem(data, "sampled_at");
                std::string sampled_at = cJSON_IsString(sampled_at_item) ? sampled_at_item->valuestring : "--:--:--";
                float cpu_percent = number_or_missing("cpu_used_percent");
                float disk_percent = number_or_missing("disk_used_percent");
                float network_rx_kbps = number_or_missing("network_rx_kbps");
                float network_tx_kbps = number_or_missing("network_tx_kbps");
                ESP_LOGI(TAG, "Server dashboard: CPU %.1f%% disk %.1f%% RX %.1f kbps TX %.1f kbps",
                    cpu_percent, disk_percent, network_rx_kbps, network_tx_kbps);
                Schedule([this, display, cpu_percent, disk_percent, network_rx_kbps, network_tx_kbps, sampled_at]() {
                    display->HideClock();
                    pomodoro_page_visible_ = false;
                    display->HidePomodoro();
                    display->ShowServerStatus(cpu_percent, disk_percent, network_rx_kbps, network_tx_kbps, sampled_at.c_str());
                });
            } else if (!cJSON_IsString(state) || strcmp(state->valuestring, "active") != 0) {
                ESP_LOGW(TAG, "Server status message is missing data");
            }
            if (cJSON_IsString(state) && strcmp(state->valuestring, "active") == 0) {
                Schedule([this]() {
                    server_dashboard_active_ = true;
                    pomodoro_page_visible_ = false;
                    Board::GetInstance().GetDisplay()->HidePomodoro();
                    audio_service_.EnableVoiceProcessing(false);
                    audio_service_.EnableWakeWordDetection(true);
                    ESP_LOGI(TAG, "Server dashboard passive mode enabled");
                });
            }
        } else if (strcmp(type->valuestring, "llm") == 0) {
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(emotion)) {
                Schedule([display, emotion_str = std::string(emotion->valuestring)]() {
                    display->SetEmotion(emotion_str.c_str());
                });
            }
        } else if (strcmp(type->valuestring, "mcp") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            if (cJSON_IsObject(payload)) {
                McpServer::GetInstance().ParseMessage(payload);
            }
        } else if (strcmp(type->valuestring, "system") == 0) {
            auto command = cJSON_GetObjectItem(root, "command");
            if (cJSON_IsString(command)) {
                ESP_LOGI(TAG, "System command: %s", command->valuestring);
                if (strcmp(command->valuestring, "reboot") == 0) {
                    // Do a reboot if user requests a OTA update
                    Schedule([this]() {
                        Reboot();
                    });
                } else {
                    ESP_LOGW(TAG, "Unknown system command: %s", command->valuestring);
                }
            }
        } else if (strcmp(type->valuestring, "alert") == 0) {
            auto status = cJSON_GetObjectItem(root, "status");
            auto message = cJSON_GetObjectItem(root, "message");
            auto emotion = cJSON_GetObjectItem(root, "emotion");
            if (cJSON_IsString(status) && cJSON_IsString(message) && cJSON_IsString(emotion)) {
                Alert(status->valuestring, message->valuestring, emotion->valuestring, Lang::Sounds::OGG_VIBRATION);
            } else {
                ESP_LOGW(TAG, "Alert command requires status, message and emotion");
            }
#if CONFIG_RECEIVE_CUSTOM_MESSAGE
        } else if (strcmp(type->valuestring, "custom") == 0) {
            auto payload = cJSON_GetObjectItem(root, "payload");
            ESP_LOGI(TAG, "Received custom message: %s", cJSON_PrintUnformatted(root));
            if (cJSON_IsObject(payload)) {
                Schedule([this, display, payload_str = std::string(cJSON_PrintUnformatted(payload))]() {
                    display->SetChatMessage("system", payload_str.c_str());
                });
            } else {
                ESP_LOGW(TAG, "Invalid custom message format: missing payload");
            }
#endif
        } else {
            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);
        }
    });
    
    protocol_->Start();
}

void Application::ShowActivationCode(const std::string& code, const std::string& message) {
    struct digit_sound {
        char digit;
        const std::string_view& sound;
    };
    static const std::array<digit_sound, 10> digit_sounds{{
        digit_sound{'0', Lang::Sounds::OGG_0},
        digit_sound{'1', Lang::Sounds::OGG_1}, 
        digit_sound{'2', Lang::Sounds::OGG_2},
        digit_sound{'3', Lang::Sounds::OGG_3},
        digit_sound{'4', Lang::Sounds::OGG_4},
        digit_sound{'5', Lang::Sounds::OGG_5},
        digit_sound{'6', Lang::Sounds::OGG_6},
        digit_sound{'7', Lang::Sounds::OGG_7},
        digit_sound{'8', Lang::Sounds::OGG_8},
        digit_sound{'9', Lang::Sounds::OGG_9}
    }};

    // This sentence uses 9KB of SRAM, so we need to wait for it to finish
    Alert(Lang::Strings::ACTIVATION, message.c_str(), "link", Lang::Sounds::OGG_ACTIVATION);

    for (const auto& digit : code) {
        auto it = std::find_if(digit_sounds.begin(), digit_sounds.end(),
            [digit](const digit_sound& ds) { return ds.digit == digit; });
        if (it != digit_sounds.end()) {
            audio_service_.PlaySound(it->sound);
        }
    }
}

void Application::Alert(const char* status, const char* message, const char* emotion, const std::string_view& sound) {
    ESP_LOGW(TAG, "Alert [%s] %s: %s", emotion, status, message);
    auto display = Board::GetInstance().GetDisplay();
    display->SetStatus(status);
    display->SetEmotion(emotion);
    display->SetChatMessage("system", message);
    if (!sound.empty()) {
        audio_service_.PlaySound(sound);
    }
}

void Application::DismissAlert() {
    if (GetDeviceState() == kDeviceStateIdle) {
        auto display = Board::GetInstance().GetDisplay();
        display->SetStatus(Lang::Strings::STANDBY);
        display->SetEmotion("neutral");
        display->SetChatMessage("system", "");
    }
}

void Application::ToggleChatState() {
    xEventGroupSetBits(event_group_, MAIN_EVENT_TOGGLE_CHAT);
}

void Application::StartListening() {
    xEventGroupSetBits(event_group_, MAIN_EVENT_START_LISTENING);
}

void Application::StopListening() {
    xEventGroupSetBits(event_group_, MAIN_EVENT_STOP_LISTENING);
}

void Application::HandleToggleChatEvent() {
    auto state = GetDeviceState();
    
    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    } else if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }

    if (state == kDeviceStateIdle) {
        ListeningMode mode = GetDefaultListeningMode();
        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            Schedule([this, mode]() {
                ContinueOpenAudioChannel(mode);
            });
            return;
        }
        SetListeningMode(mode);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
    } else if (state == kDeviceStateListening) {
        protocol_->CloseAudioChannel();
    }
}

void Application::ContinueOpenAudioChannel(ListeningMode mode) {
    // Check state again in case it was changed during scheduling
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    if (!protocol_->IsAudioChannelOpened()) {
        if (!protocol_->OpenAudioChannel()) {
            return;
        }
    }

    SetListeningMode(mode);
}

void Application::HandleStartListeningEvent() {
    if (reminder_page_visible_) {
        DismissReminder();
    }
    auto state = GetDeviceState();
    
    if (state == kDeviceStateActivating) {
        SetDeviceState(kDeviceStateIdle);
        return;
    } else if (state == kDeviceStateWifiConfiguring) {
        audio_service_.EnableAudioTesting(true);
        SetDeviceState(kDeviceStateAudioTesting);
        return;
    }

    if (!protocol_) {
        ESP_LOGE(TAG, "Protocol not initialized");
        return;
    }
    
    if (state == kDeviceStateIdle) {
        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            Schedule([this]() {
                ContinueOpenAudioChannel(kListeningModeManualStop);
            });
            return;
        }
        SetListeningMode(kListeningModeManualStop);
    } else if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
        SetListeningMode(kListeningModeManualStop);
    }
}

void Application::HandleStopListeningEvent() {
    auto state = GetDeviceState();
    
    if (state == kDeviceStateAudioTesting) {
        audio_service_.EnableAudioTesting(false);
        SetDeviceState(kDeviceStateWifiConfiguring);
        return;
    } else if (state == kDeviceStateListening) {
        if (protocol_) {
            protocol_->SendStopListening();
        }
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::HandleWakeWordDetectedEvent() {
    if (reminder_page_visible_) {
        DismissReminder();
    }
    // A wake word is also the explicit exit action for persistent tool dashboards.
    auto display = Board::GetInstance().GetDisplay();
    display->HideServerStatus();
    if (pomodoro_timer_.state() == PomodoroState::kFinished) {
        pomodoro_timer_.Cancel();
    }
    SuspendPomodoroPage();
    if (!protocol_) {
        return;
    }

    auto state = GetDeviceState();
    auto wake_word = audio_service_.GetLastWakeWord();
    ESP_LOGI(TAG, "Wake word detected: %s (state: %d)", wake_word.c_str(), (int)state);

    if (server_dashboard_active_) {
        server_dashboard_active_ = false;
        AbortSpeaking(kAbortReasonWakeWordDetected);
        audio_service_.ResetDecoder();
        uint32_t dropped_frames = 0;
        while (audio_service_.PopPacketFromSendQueue()) {
            ++dropped_frames;
        }
        CompleteBargeInLocalClear(dropped_frames);
        // Force a real state transition so HandleStateChangedEvent restarts
        // voice processing even though the dashboard was parked in listening state.
        SetDeviceState(kDeviceStateIdle);
        play_popup_on_listening_ = true;
        SetListeningMode(GetDefaultListeningMode());
        ESP_LOGI(TAG, "Server dashboard dismissed by wake word");
        return;
    }

    if (state == kDeviceStateIdle) {
        audio_service_.EncodeWakeWord();
        auto wake_word = audio_service_.GetLastWakeWord();

        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update),
            // then continue with OpenAudioChannel which may block for ~1 second
            Schedule([this, wake_word]() {
                ContinueWakeWordInvoke(wake_word);
            });
            return;
        }
        // Channel already opened, continue directly
        ContinueWakeWordInvoke(wake_word);
    } else if (state == kDeviceStateSpeaking || state == kDeviceStateListening) {
        AbortSpeaking(kAbortReasonWakeWordDetected);
        // Drop any Opus packets and decoded PCM already buffered on the device.
        // Otherwise the listening-state transition waits for the old response
        // to finish playing, which makes wake-word barge-in appear delayed.
        audio_service_.ResetDecoder();
        // Clear send queue to avoid sending residues to server
        uint32_t dropped_frames = 0;
        while (audio_service_.PopPacketFromSendQueue()) {
            ++dropped_frames;
        }
        CompleteBargeInLocalClear(dropped_frames);

        if (state == kDeviceStateListening) {
            protocol_->SendStartListening(GetDefaultListeningMode());
            audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
            // Re-enable wake word detection as it was stopped by the detection itself
            audio_service_.EnableWakeWordDetection(true);
        } else {
            // Play popup sound and start listening again
            play_popup_on_listening_ = true;
            SetListeningMode(GetDefaultListeningMode());
        }
    } else if (state == kDeviceStateActivating) {
        // Restart the activation check if the wake word is detected during activation
        SetDeviceState(kDeviceStateIdle);
    }
}

void Application::ContinueWakeWordInvoke(const std::string& wake_word) {
    // Check state again in case it was changed during scheduling
    if (GetDeviceState() != kDeviceStateConnecting) {
        return;
    }

    if (!protocol_->IsAudioChannelOpened()) {
        if (!protocol_->OpenAudioChannel()) {
            audio_service_.EnableWakeWordDetection(true);
            return;
        }
    }

    ESP_LOGI(TAG, "Wake word detected: %s", wake_word.c_str());
#if CONFIG_SEND_WAKE_WORD_DATA
    // Encode and send the wake word data to the server
    while (auto packet = audio_service_.PopWakeWordPacket()) {
        protocol_->SendAudio(std::move(packet));
    }
    // Set the chat state to wake word detected
    protocol_->SendWakeWordDetected(wake_word);
    SetListeningMode(GetDefaultListeningMode());
#else
    // Set flag to play popup sound after state changes to listening
    // (PlaySound here would be cleared by ResetDecoder in EnableVoiceProcessing)
    play_popup_on_listening_ = true;
    SetListeningMode(GetDefaultListeningMode());
#endif
}

void Application::HandleStateChangedEvent() {
    DeviceState new_state = state_machine_.GetState();
    clock_ticks_ = 0;

    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();
    auto led = board.GetLed();
    led->OnStateChanged();
    if (new_state != kDeviceStateIdle) {
        display->HideClock();
    }
    
    switch (new_state) {
        case kDeviceStateUnknown:
            break;
        case kDeviceStateIdle:
            display->SetStatus(Lang::Strings::STANDBY);
            display->ClearChatMessages();  // Clear messages first
            display->SetEmotion("neutral"); // Then set emotion (wechat mode checks child count)
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(true);
            UpdateClockFace();
            break;
        case kDeviceStateConnecting:
            display->SetStatus(Lang::Strings::CONNECTING);
            display->SetEmotion("neutral");
            display->SetChatMessage("system", "");
            break;
        case kDeviceStateListening:
            display->SetStatus(Lang::Strings::LISTENING);
            display->SetEmotion("neutral");

            // Make sure the audio processor is running
            if (play_popup_on_listening_ || !audio_service_.IsAudioProcessorRunning()) {
                // For auto mode, wait for playback queue to be empty before enabling voice processing
                // This prevents audio truncation when STOP arrives late due to network jitter
                if (listening_mode_ == kListeningModeAutoStop) {
                    audio_service_.WaitForPlaybackQueueEmpty();
                }
                
                // Send the start listening command
                protocol_->SendStartListening(listening_mode_);
                audio_service_.EnableVoiceProcessing(true);
            }

#ifdef CONFIG_WAKE_WORD_DETECTION_IN_LISTENING
            // Enable wake word detection in listening mode (configured via Kconfig)
            audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
#else
            // Disable wake word detection in listening mode
            audio_service_.EnableWakeWordDetection(false);
#endif
            
            // Play popup sound after ResetDecoder (in EnableVoiceProcessing) has been called
            if (play_popup_on_listening_) {
                play_popup_on_listening_ = false;
                audio_service_.PlaySound(Lang::Sounds::OGG_POPUP);
            }
            break;
        case kDeviceStateSpeaking:
            display->SetStatus(Lang::Strings::SPEAKING);

            if (listening_mode_ != kListeningModeRealtime) {
                audio_service_.EnableVoiceProcessing(false);
                // ESP32-C5 uses the lightweight EspWakeWord implementation rather
                // than AfeWakeWord. Keep wake-word detection active while audio is
                // playing so saying the wake word can send an abort and start a
                // new turn. Ordinary microphone streaming remains disabled.
#if CONFIG_IDF_TARGET_ESP32C5
                audio_service_.EnableWakeWordDetection(true);
#else
                // Other targets require AFE wake-word support during playback.
                audio_service_.EnableWakeWordDetection(audio_service_.IsAfeWakeWord());
#endif
            }
            audio_service_.ResetDecoder();
            break;
        case kDeviceStateWifiConfiguring:
            audio_service_.EnableVoiceProcessing(false);
            audio_service_.EnableWakeWordDetection(false);
            break;
        default:
            // Do nothing
            break;
    }
}

void Application::Schedule(std::function<void()>&& callback) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        main_tasks_.push_back(std::move(callback));
    }
    xEventGroupSetBits(event_group_, MAIN_EVENT_SCHEDULE);
}

void Application::AbortSpeaking(AbortReason reason) {
    ESP_LOGI(TAG, "Abort speaking");
    aborted_ = true;
    if (protocol_) {
        if (reason == kAbortReasonWakeWordDetected && GetDeviceState() == kDeviceStateSpeaking) {
            if (++barge_in_metric_id_ == 0) {
                ++barge_in_metric_id_;
            }
            barge_in_abort_sent_us_ = esp_timer_get_time();
            protocol_->SendAbortSpeaking(reason, barge_in_metric_id_,
                barge_in_abort_sent_us_ / 1000);
            ESP_LOGI(TAG, "Barge-in abort sent id=%lu uptime_ms=%lld",
                static_cast<unsigned long>(barge_in_metric_id_),
                static_cast<long long>(barge_in_abort_sent_us_ / 1000));
        } else {
            protocol_->SendAbortSpeaking(reason);
        }
    }
}

void Application::CompleteBargeInLocalClear(uint32_t uplink_frames_dropped) {
    if (barge_in_abort_sent_us_ <= 0) {
        return;
    }
    const int64_t cleared_us = esp_timer_get_time();
    const int64_t elapsed_us = cleared_us >= barge_in_abort_sent_us_
        ? cleared_us - barge_in_abort_sent_us_ : 0;
    barge_in_local_clear_ms_ = static_cast<uint32_t>(elapsed_us / 1000);
    barge_in_uplink_frames_dropped_ = uplink_frames_dropped;
    barge_in_free_sram_bytes_ = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    barge_in_min_free_sram_bytes_ = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    wifi_ap_record_t ap_info = {};
    barge_in_wifi_rssi_dbm_ = esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK ? ap_info.rssi : 0;
    ESP_LOGI(TAG,
        "Barge-in audio cleared id=%lu local_ms=%lu rssi_dbm=%d free_sram=%lu min_free_sram=%lu uplink_dropped=%lu",
        static_cast<unsigned long>(barge_in_metric_id_),
        static_cast<unsigned long>(barge_in_local_clear_ms_),
        barge_in_wifi_rssi_dbm_,
        static_cast<unsigned long>(barge_in_free_sram_bytes_),
        static_cast<unsigned long>(barge_in_min_free_sram_bytes_),
        static_cast<unsigned long>(barge_in_uplink_frames_dropped_));
}

void Application::ReportBargeInStopReceived(int64_t received_us) {
    if (barge_in_abort_sent_us_ <= 0 || received_us < barge_in_abort_sent_us_) {
        return;
    }
    const uint32_t round_trip_ms = static_cast<uint32_t>(
        (received_us - barge_in_abort_sent_us_) / 1000);
    ESP_LOGI(TAG, "Barge-in stop received id=%lu round_trip_ms=%lu",
        static_cast<unsigned long>(barge_in_metric_id_),
        static_cast<unsigned long>(round_trip_ms));
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        protocol_->SendBargeInMetric(
            barge_in_metric_id_, round_trip_ms, barge_in_local_clear_ms_,
            barge_in_wifi_rssi_dbm_, barge_in_free_sram_bytes_,
            barge_in_min_free_sram_bytes_, barge_in_uplink_frames_dropped_);
    }
    barge_in_abort_sent_us_ = 0;
}

void Application::SetListeningMode(ListeningMode mode) {
    listening_mode_ = mode;
    SetDeviceState(kDeviceStateListening);
}

ListeningMode Application::GetDefaultListeningMode() const {
    return aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime;
}

void Application::Reboot() {
    ESP_LOGI(TAG, "Rebooting...");
    // Disconnect the audio channel
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        protocol_->CloseAudioChannel();
    }
    protocol_.reset();
    audio_service_.Stop();

    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
}

bool Application::UpgradeFirmware(const std::string& url, const std::string& version) {
    auto& board = Board::GetInstance();
    auto display = board.GetDisplay();

    std::string upgrade_url = url;
    std::string version_info = version.empty() ? "(Manual upgrade)" : version;

    // Close audio channel if it's open
    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        ESP_LOGI(TAG, "Closing audio channel before firmware upgrade");
        protocol_->CloseAudioChannel();
    }
    ESP_LOGI(TAG, "Starting firmware upgrade from URL: %s", upgrade_url.c_str());

    Alert(Lang::Strings::OTA_UPGRADE, Lang::Strings::UPGRADING, "download", Lang::Sounds::OGG_UPGRADE);
    vTaskDelay(pdMS_TO_TICKS(3000));

    SetDeviceState(kDeviceStateUpgrading);

    std::string message = std::string(Lang::Strings::NEW_VERSION) + version_info;
    display->SetChatMessage("system", message.c_str());

    board.SetPowerSaveLevel(PowerSaveLevel::PERFORMANCE);
    audio_service_.Stop();
    vTaskDelay(pdMS_TO_TICKS(1000));

    bool upgrade_success = Ota::Upgrade(upgrade_url, [this, display](int progress, size_t speed) {
        char buffer[32];
        snprintf(buffer, sizeof(buffer), "%d%% %uKB/s", progress, speed / 1024);
        Schedule([display, message = std::string(buffer)]() {
            display->SetChatMessage("system", message.c_str());
        });
    });

    if (!upgrade_success) {
        // Upgrade failed, restart audio service and continue running
        ESP_LOGE(TAG, "Firmware upgrade failed, restarting audio service and continuing operation...");
        audio_service_.Start(); // Restart audio service
        board.SetPowerSaveLevel(PowerSaveLevel::LOW_POWER); // Restore power save level
        Alert(Lang::Strings::ERROR, Lang::Strings::UPGRADE_FAILED, "circle_xmark", Lang::Sounds::OGG_EXCLAMATION);
        vTaskDelay(pdMS_TO_TICKS(3000));
        return false;
    } else {
        // Upgrade success, reboot immediately
        ESP_LOGI(TAG, "Firmware upgrade successful, rebooting...");
        display->SetChatMessage("system", "Upgrade successful, rebooting...");
        vTaskDelay(pdMS_TO_TICKS(1000)); // Brief pause to show message
        Reboot();
        return true;
    }
}

void Application::WakeWordInvoke(const std::string& wake_word) {
    if (!protocol_) {
        return;
    }

    auto state = GetDeviceState();
    
    if (state == kDeviceStateIdle) {
        audio_service_.EncodeWakeWord();

        if (!protocol_->IsAudioChannelOpened()) {
            SetDeviceState(kDeviceStateConnecting);
            // Schedule to let the state change be processed first (UI update)
            Schedule([this, wake_word]() {
                ContinueWakeWordInvoke(wake_word);
            });
            return;
        }
        // Channel already opened, continue directly
        ContinueWakeWordInvoke(wake_word);
    } else if (state == kDeviceStateSpeaking) {
        Schedule([this]() {
            AbortSpeaking(kAbortReasonNone);
        });
    } else if (state == kDeviceStateListening) {   
        Schedule([this]() {
            if (protocol_) {
                protocol_->CloseAudioChannel();
            }
        });
    }
}

bool Application::CanEnterSleepMode() {
    if (GetDeviceState() != kDeviceStateIdle) {
        return false;
    }

    if (protocol_ && protocol_->IsAudioChannelOpened()) {
        return false;
    }

    if (!audio_service_.IsIdle()) {
        return false;
    }

    // Now it is safe to enter sleep mode
    return true;
}

void Application::SendMcpMessage(const std::string& payload) {
    // Always schedule to run in main task for thread safety
    Schedule([this, payload = std::move(payload)]() {
        if (protocol_) {
            protocol_->SendMcpMessage(payload);
        }
    });
}

void Application::SetAecMode(AecMode mode) {
    aec_mode_ = mode;
    Schedule([this]() {
        auto& board = Board::GetInstance();
        auto display = board.GetDisplay();
        switch (aec_mode_) {
        case kAecOff:
            audio_service_.EnableDeviceAec(false);
            display->ShowNotification(Lang::Strings::RTC_MODE_OFF);
            break;
        case kAecOnServerSide:
            audio_service_.EnableDeviceAec(false);
            display->ShowNotification(Lang::Strings::RTC_MODE_ON);
            break;
        case kAecOnDeviceSide:
            audio_service_.EnableDeviceAec(true);
            display->ShowNotification(Lang::Strings::RTC_MODE_ON);
            break;
        }

        // If the AEC mode is changed, close the audio channel
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
    });
}

void Application::HandlePomodoroCommand(const std::string& action, int duration_seconds, const std::string& label) {
    const int64_t now_us = esp_timer_get_time();
    bool changed = false;
    if (action == "start") {
        changed = pomodoro_timer_.Start(duration_seconds, now_us);
        if (changed) {
            pomodoro_label_ = label.empty() ? "番茄钟" : label.substr(0, 40);
        }
    } else if (action == "pause") {
        changed = pomodoro_timer_.Pause(now_us);
    } else if (action == "resume") {
        changed = pomodoro_timer_.Resume(now_us);
    } else if (action == "cancel") {
        pomodoro_timer_.Cancel();
        pomodoro_page_visible_ = false;
        Board::GetInstance().GetDisplay()->HidePomodoro();
        ESP_LOGI(TAG, "Pomodoro cancelled");
        if (GetDeviceState() == kDeviceStateIdle) {
            UpdateClockFace();
        }
        return;
    } else if (action == "show" || action == "status") {
        changed = pomodoro_timer_.state() != PomodoroState::kIdle;
    } else {
        ESP_LOGW(TAG, "Unknown Pomodoro action: %s", action.c_str());
        return;
    }

    if (!changed) {
        ESP_LOGW(TAG, "Pomodoro action rejected: %s", action.c_str());
        return;
    }
    server_dashboard_active_ = false;
    auto display = Board::GetInstance().GetDisplay();
    display->HideServerStatus();
    pomodoro_page_visible_ = true;
    ShowPomodoroPage();
    ESP_LOGI(TAG, "Pomodoro action=%s remaining=%d total=%d", action.c_str(),
        pomodoro_timer_.RemainingSeconds(now_us), pomodoro_timer_.total_seconds());
}

void Application::UpdateClockContext(const std::string& city, const std::string& condition,
        float temperature_c, int humidity_percent) {
    clock_city_ = city.empty() ? "位置未知" : city.substr(0, 40);
    clock_condition_ = condition.empty() ? "天气未知" : condition.substr(0, 40);
    clock_temperature_c_ = temperature_c;
    clock_humidity_percent_ = humidity_percent;
    ESP_LOGI(TAG, "Clock context updated: city=%s weather=%s temp=%.1f humidity=%d",
        clock_city_.c_str(), clock_condition_.c_str(),
        clock_temperature_c_, clock_humidity_percent_);
    UpdateClockFace();
}

void Application::RefreshClockWeather() {
    if (clock_weather_task_handle_ != nullptr) {
        return;
    }

    // Record the attempt before starting the task so a failed request cannot create
    // a tight retry loop on the one-second clock tick.
    last_clock_weather_refresh_us_ = esp_timer_get_time();
    const BaseType_t created = xTaskCreate([](void* arg) {
        auto* app = static_cast<Application*>(arg);
        Ota clock_context;
        const esp_err_t err = clock_context.RefreshClockContext();
        if (err == ESP_OK && clock_context.HasClockContext()) {
            const std::string city = clock_context.GetClockCity();
            const std::string condition = clock_context.GetClockCondition();
            const float temperature_c = clock_context.GetClockTemperatureC();
            const int humidity_percent = clock_context.GetClockHumidityPercent();
            app->Schedule([app, city, condition, temperature_c, humidity_percent]() {
                app->UpdateClockContext(city, condition, temperature_c, humidity_percent);
            });
        } else {
            ESP_LOGW(TAG, "Automatic clock weather refresh failed: %s", esp_err_to_name(err));
        }
        app->clock_weather_task_handle_ = nullptr;
        vTaskDelete(nullptr);
    }, "clock_weather", 4096 * 2, this, 2, &clock_weather_task_handle_);

    if (created != pdPASS) {
        clock_weather_task_handle_ = nullptr;
        ESP_LOGW(TAG, "Failed to create automatic clock weather refresh task");
    }
}

void Application::UpdateClockFace() {
    if (GetDeviceState() != kDeviceStateIdle || server_dashboard_active_ ||
            pomodoro_page_visible_ || reminder_page_visible_) {
        return;
    }

    char time_text[8] = "--:--";
    char date_text[32] = "---- -- --";
    const time_t now = time(nullptr);
    if (has_server_time_ && now > 1700000000) {
        struct tm local_time = {};
        localtime_r(&now, &local_time);
        static const char* weekdays[] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
        snprintf(time_text, sizeof(time_text), "%02d:%02d", local_time.tm_hour, local_time.tm_min);
        snprintf(date_text, sizeof(date_text), "%04d-%02d-%02d  %s",
            local_time.tm_year + 1900, local_time.tm_mon + 1, local_time.tm_mday,
            weekdays[local_time.tm_wday]);
    }

    int battery_percent = -1;
    bool charging = false;
    bool discharging = false;
    auto& board = Board::GetInstance();
    board.GetBatteryLevel(battery_percent, charging, discharging);
    board.GetDisplay()->ShowClock(
        time_text, date_text, battery_percent, charging,
        clock_city_.c_str(), clock_condition_.c_str(),
        clock_temperature_c_, clock_humidity_percent_);
}

void Application::UpdatePomodoroTimer() {
    const int64_t now_us = esp_timer_get_time();
    if (pomodoro_timer_.Tick(now_us)) {
        FinishPomodoro();
        return;
    }
    if (pomodoro_page_visible_ && pomodoro_timer_.state() != PomodoroState::kIdle) {
        ShowPomodoroPage();
    }
}

void Application::ShowPomodoroPage() {
    if (!pomodoro_page_visible_ || server_dashboard_active_) {
        return;
    }
    const char* state = "running";
    if (pomodoro_timer_.state() == PomodoroState::kPaused) {
        state = "paused";
    } else if (pomodoro_timer_.state() == PomodoroState::kFinished) {
        state = "finished";
    } else if (pomodoro_timer_.state() == PomodoroState::kIdle) {
        return;
    }
    Board::GetInstance().GetDisplay()->ShowPomodoro(
        state, pomodoro_timer_.RemainingSeconds(esp_timer_get_time()),
        pomodoro_timer_.total_seconds(), pomodoro_label_.c_str());
}

void Application::SuspendPomodoroPage() {
    pomodoro_page_visible_ = false;
    Board::GetInstance().GetDisplay()->HidePomodoro();
}

void Application::FinishPomodoro() {
    ESP_LOGI(TAG, "Pomodoro finished: %s", pomodoro_label_.c_str());
    server_dashboard_active_ = false;
    auto display = Board::GetInstance().GetDisplay();
    display->HideClock();
    display->HideServerStatus();
    pomodoro_page_visible_ = true;

    auto state = GetDeviceState();
    if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
        audio_service_.ResetDecoder();
        SetDeviceState(kDeviceStateIdle);
    } else if (state == kDeviceStateListening) {
        if (protocol_) {
            protocol_->SendStopListening();
        }
        SetDeviceState(kDeviceStateIdle);
    }
    ShowPomodoroPage();
    audio_service_.PlaySound(Lang::Sounds::OGG_VIBRATION);
}

void Application::HandleReminderCommand(const std::string& action, int id,
        int64_t trigger_at_epoch, const std::string& title, const std::string& kind) {
    if (action == "schedule") {
        if (!reminder_scheduler_.Schedule(id, trigger_at_epoch, title, kind)) {
            ESP_LOGW(TAG, "Reminder schedule rejected id=%d", id);
        }
        return;
    }
    if (action == "cancel") {
        reminder_scheduler_.Cancel(id);
        if (reminder_page_visible_ && active_reminder_.id == id) {
            DismissReminder();
        }
        return;
    }
    ESP_LOGW(TAG, "Unknown reminder action: %s", action.c_str());
}

void Application::UpdateReminderSchedule() {
    if (reminder_page_visible_) {
        reminder_ring_ticks_++;
        if (active_reminder_.kind == "alarm" && reminder_ring_ticks_ % 10 == 0) {
            audio_service_.PlaySound(Lang::Sounds::OGG_EXCLAMATION);
        }
        return;
    }
    ScheduledReminder due;
    if (!reminder_scheduler_.PopDue(static_cast<int64_t>(time(nullptr)), due)) {
        return;
    }
    active_reminder_ = std::move(due);
    reminder_page_visible_ = true;
    reminder_ring_ticks_ = 0;
    server_dashboard_active_ = false;
    pomodoro_page_visible_ = false;

    auto display = Board::GetInstance().GetDisplay();
    display->HideClock();
    display->HideServerStatus();
    display->HidePomodoro();

    const auto state = GetDeviceState();
    if (state == kDeviceStateSpeaking) {
        AbortSpeaking(kAbortReasonNone);
        audio_service_.ResetDecoder();
        SetDeviceState(kDeviceStateIdle);
    } else if (state == kDeviceStateListening) {
        if (protocol_) {
            protocol_->SendStopListening();
        }
        SetDeviceState(kDeviceStateIdle);
    }
    ShowReminderPage();
    audio_service_.PlaySound(Lang::Sounds::OGG_EXCLAMATION);
    ESP_LOGI(TAG, "Reminder fired id=%d kind=%s title=%s", active_reminder_.id,
        active_reminder_.kind.c_str(), active_reminder_.title.c_str());
}

void Application::ShowReminderPage() {
    if (!reminder_page_visible_) {
        return;
    }
    char time_text[8] = "--:--";
    const time_t now = time(nullptr);
    if (now > 1700000000) {
        struct tm local_time = {};
        localtime_r(&now, &local_time);
        snprintf(time_text, sizeof(time_text), "%02d:%02d", local_time.tm_hour,
            local_time.tm_min);
    }
    Board::GetInstance().GetDisplay()->ShowReminder(active_reminder_.kind.c_str(),
        active_reminder_.title.c_str(), time_text);
}

void Application::DismissReminder() {
    if (!reminder_page_visible_) {
        return;
    }
    reminder_page_visible_ = false;
    reminder_ring_ticks_ = 0;
    audio_service_.ResetDecoder();
    Board::GetInstance().GetDisplay()->HideReminder();
    ESP_LOGI(TAG, "Reminder dismissed id=%d", active_reminder_.id);
    active_reminder_ = ScheduledReminder{};
    if (pomodoro_timer_.state() != PomodoroState::kIdle) {
        pomodoro_page_visible_ = true;
        ShowPomodoroPage();
    } else if (GetDeviceState() == kDeviceStateIdle) {
        UpdateClockFace();
    }
}

void Application::PlaySound(const std::string_view& sound) {
    audio_service_.PlaySound(sound);
}

void Application::ResetProtocol() {
    Schedule([this]() {
        // Close audio channel if opened
        if (protocol_ && protocol_->IsAudioChannelOpened()) {
            protocol_->CloseAudioChannel();
        }
        // Reset protocol
        protocol_.reset();
    });
}

