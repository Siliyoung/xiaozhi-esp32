#ifndef REMINDER_SCHEDULER_H
#define REMINDER_SCHEDULER_H

#include <cstdint>
#include <string>
#include <vector>

struct ScheduledReminder {
    int id = 0;
    int64_t trigger_at_epoch = 0;
    std::string title;
    std::string kind = "reminder";
};

class ReminderScheduler {
public:
    ReminderScheduler();

    bool Schedule(int id, int64_t trigger_at_epoch, const std::string& title,
        const std::string& kind);
    bool Cancel(int id);
    bool PopDue(int64_t now_epoch, ScheduledReminder& reminder);
    size_t size() const { return reminders_.size(); }

private:
    static constexpr size_t kMaxReminders = 16;
    std::vector<ScheduledReminder> reminders_;

    void Load();
    void Save() const;
    void Sort();
};

#endif
