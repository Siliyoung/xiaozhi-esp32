#include "reminder_scheduler.h"

#include "settings.h"

#include <algorithm>
#include <cJSON.h>
#include <esp_log.h>

#define TAG "ReminderScheduler"

ReminderScheduler::ReminderScheduler() {
    Load();
}

void ReminderScheduler::Sort() {
    std::sort(reminders_.begin(), reminders_.end(),
        [](const ScheduledReminder& left, const ScheduledReminder& right) {
            return left.trigger_at_epoch < right.trigger_at_epoch;
        });
}

void ReminderScheduler::Load() {
    Settings settings("reminders", false);
    const std::string serialized = settings.GetString("items");
    if (serialized.empty()) {
        return;
    }
    cJSON* root = cJSON_Parse(serialized.c_str());
    if (!cJSON_IsArray(root)) {
        cJSON_Delete(root);
        ESP_LOGW(TAG, "Stored reminder list is invalid; ignoring it");
        return;
    }
    cJSON* item = nullptr;
    cJSON_ArrayForEach(item, root) {
        auto id = cJSON_GetObjectItem(item, "id");
        auto trigger = cJSON_GetObjectItem(item, "trigger_at_epoch");
        auto title = cJSON_GetObjectItem(item, "title");
        auto kind = cJSON_GetObjectItem(item, "kind");
        if (!cJSON_IsNumber(id) || !cJSON_IsNumber(trigger) || !cJSON_IsString(title)) {
            continue;
        }
        ScheduledReminder reminder;
        reminder.id = id->valueint;
        reminder.trigger_at_epoch = static_cast<int64_t>(trigger->valuedouble);
        reminder.title = title->valuestring;
        reminder.kind = cJSON_IsString(kind) ? kind->valuestring : "reminder";
        if (reminder.id > 0 && reminder.trigger_at_epoch > 0 &&
                reminders_.size() < kMaxReminders) {
            reminders_.push_back(std::move(reminder));
        }
    }
    cJSON_Delete(root);
    Sort();
    ESP_LOGI(TAG, "Loaded %u persisted reminders", static_cast<unsigned>(reminders_.size()));
}

void ReminderScheduler::Save() const {
    cJSON* root = cJSON_CreateArray();
    for (const auto& reminder : reminders_) {
        cJSON* item = cJSON_CreateObject();
        cJSON_AddNumberToObject(item, "id", reminder.id);
        cJSON_AddNumberToObject(item, "trigger_at_epoch",
            static_cast<double>(reminder.trigger_at_epoch));
        cJSON_AddStringToObject(item, "title", reminder.title.c_str());
        cJSON_AddStringToObject(item, "kind", reminder.kind.c_str());
        cJSON_AddItemToArray(root, item);
    }
    char* serialized = cJSON_PrintUnformatted(root);
    if (serialized != nullptr) {
        Settings settings("reminders", true);
        settings.SetString("items", serialized);
        cJSON_free(serialized);
    }
    cJSON_Delete(root);
}

bool ReminderScheduler::Schedule(int id, int64_t trigger_at_epoch,
        const std::string& title, const std::string& kind) {
    if (id == 0 || trigger_at_epoch <= 0 || title.empty()) {
        return false;
    }
    auto existing = std::find_if(reminders_.begin(), reminders_.end(),
        [id](const ScheduledReminder& reminder) { return reminder.id == id; });
    if (existing == reminders_.end()) {
        if (reminders_.size() >= kMaxReminders) {
            ESP_LOGW(TAG, "Reminder capacity reached (%u)", static_cast<unsigned>(kMaxReminders));
            return false;
        }
        reminders_.push_back({id, trigger_at_epoch, title.substr(0, 120), kind});
    } else {
        existing->trigger_at_epoch = trigger_at_epoch;
        existing->title = title.substr(0, 120);
        existing->kind = kind;
    }
    Sort();
    Save();
    ESP_LOGI(TAG, "Scheduled reminder id=%d trigger=%lld", id,
        static_cast<long long>(trigger_at_epoch));
    return true;
}

bool ReminderScheduler::Cancel(int id) {
    const auto old_size = reminders_.size();
    reminders_.erase(
        std::remove_if(reminders_.begin(), reminders_.end(),
            [id](const ScheduledReminder& reminder) { return reminder.id == id; }),
        reminders_.end());
    if (reminders_.size() == old_size) {
        return false;
    }
    Save();
    ESP_LOGI(TAG, "Cancelled reminder id=%d", id);
    return true;
}

bool ReminderScheduler::PopDue(int64_t now_epoch, ScheduledReminder& reminder) {
    if (reminders_.empty() || now_epoch < 1700000000 ||
            reminders_.front().trigger_at_epoch > now_epoch) {
        return false;
    }
    reminder = reminders_.front();
    reminders_.erase(reminders_.begin());
    Save();
    return true;
}
