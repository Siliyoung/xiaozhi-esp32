#ifndef POMODORO_TIMER_H
#define POMODORO_TIMER_H

#include <cstdint>

enum class PomodoroState {
    kIdle,
    kRunning,
    kPaused,
    kFinished,
};

class PomodoroTimer {
public:
    bool Start(int duration_seconds, int64_t now_us);
    bool Pause(int64_t now_us);
    bool Resume(int64_t now_us);
    void Cancel();
    bool Tick(int64_t now_us);

    PomodoroState state() const { return state_; }
    int total_seconds() const { return total_seconds_; }
    int RemainingSeconds(int64_t now_us) const;
    bool IsActive() const {
        return state_ == PomodoroState::kRunning || state_ == PomodoroState::kPaused;
    }

private:
    PomodoroState state_ = PomodoroState::kIdle;
    int total_seconds_ = 0;
    int paused_remaining_seconds_ = 0;
    int64_t deadline_us_ = 0;
};

#endif  // POMODORO_TIMER_H
