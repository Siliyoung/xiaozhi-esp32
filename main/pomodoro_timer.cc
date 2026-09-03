#include "pomodoro_timer.h"

#include <algorithm>

bool PomodoroTimer::Start(int duration_seconds, int64_t now_us) {
    if (duration_seconds < 1 || duration_seconds > 12 * 60 * 60) {
        return false;
    }
    state_ = PomodoroState::kRunning;
    total_seconds_ = duration_seconds;
    paused_remaining_seconds_ = duration_seconds;
    deadline_us_ = now_us + static_cast<int64_t>(duration_seconds) * 1000000;
    return true;
}

bool PomodoroTimer::Pause(int64_t now_us) {
    if (state_ != PomodoroState::kRunning) {
        return false;
    }
    paused_remaining_seconds_ = RemainingSeconds(now_us);
    state_ = PomodoroState::kPaused;
    return true;
}

bool PomodoroTimer::Resume(int64_t now_us) {
    if (state_ != PomodoroState::kPaused || paused_remaining_seconds_ <= 0) {
        return false;
    }
    deadline_us_ = now_us + static_cast<int64_t>(paused_remaining_seconds_) * 1000000;
    state_ = PomodoroState::kRunning;
    return true;
}

void PomodoroTimer::Cancel() {
    state_ = PomodoroState::kIdle;
    total_seconds_ = 0;
    paused_remaining_seconds_ = 0;
    deadline_us_ = 0;
}

bool PomodoroTimer::Tick(int64_t now_us) {
    if (state_ != PomodoroState::kRunning || RemainingSeconds(now_us) > 0) {
        return false;
    }
    state_ = PomodoroState::kFinished;
    paused_remaining_seconds_ = 0;
    return true;
}

int PomodoroTimer::RemainingSeconds(int64_t now_us) const {
    if (state_ == PomodoroState::kPaused) {
        return paused_remaining_seconds_;
    }
    if (state_ != PomodoroState::kRunning) {
        return 0;
    }
    const int64_t remaining_us = std::max<int64_t>(0, deadline_us_ - now_us);
    return static_cast<int>((remaining_us + 999999) / 1000000);
}
