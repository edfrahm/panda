#pragma once

#include "board/drivers/drivers.h"

struct fan_state_t fan_state;

static const uint8_t FAN_TICK_FREQ = 8U;

#ifdef STM32F4
// The comma 3's (dos) fan does not always start from rest at a low duty, and once it has
// sat stalled for a while it stays stopped until its power is cut. As the original comma 3
// firmware's fan controller did, a fan that has not spun up yet gets a duty that climbs from
// the top of the datasheet's minimum startup duty range, and a fan that stays stalled is
// power-cycled through its enable line, with a longer wait after each try.
static const uint8_t FAN_STALL_THRESHOLD_MIN = 3U;

// Returns true on the tick the fan should be power-cycled
static bool fan_stall_tick(uint16_t fan_rpm_fast) {
  const uint8_t FAN_STALL_THRESHOLD_MAX = 8U;

  bool fan_stalled = false;
  if (fan_state.power > 0U) {
    if (fan_rpm_fast == 0U) {
      fan_state.stall_counter = MIN(fan_state.stall_counter + 1U, 254U);
    } else {
      fan_state.stall_counter = 0U;
    }

    if (fan_state.stall_counter > (fan_state.stall_threshold * FAN_TICK_FREQ)) {
      fan_stalled = true;
      fan_state.stall_counter = 0U;
      fan_state.stall_threshold = CLAMP(fan_state.stall_threshold + 2U, FAN_STALL_THRESHOLD_MIN, FAN_STALL_THRESHOLD_MAX);
    }
  } else {
    fan_state.stall_counter = 0U;
    fan_state.stall_threshold = FAN_STALL_THRESHOLD_MIN;
  }
  return fan_stalled;
}

// Returns the duty to apply while the fan is starting, or 0 once it has spun up
static uint8_t fan_start_tick(bool fan_stalled) {
  const uint8_t FAN_START_POWER = 45U;
  const uint32_t FAN_START_RAMP = 2U;      // percent per tick, 45% to 100% in about 3.5 s
  const uint16_t FAN_SPUN_UP_RPM = 500U;   // openpilot's own "fan working" threshold

  if ((fan_state.power == 0U) || fan_stalled) {
    fan_state.spun_up = false;
    fan_state.start_power = 0U;
  } else if (fan_state.rpm >= FAN_SPUN_UP_RPM) {
    fan_state.spun_up = true;
    fan_state.start_power = 0U;
  } else if (!fan_state.spun_up) {
    if (fan_state.start_power == 0U) {
      fan_state.start_power = FAN_START_POWER;
    } else {
      fan_state.start_power = MIN(fan_state.start_power + FAN_START_RAMP, 100U);
    }
  } else {
  }
  return fan_state.start_power;
}
#endif

void fan_set_power(uint8_t percentage) {
  if (percentage > 0U) {
    fan_state.power = CLAMP(percentage, 20U, 100U);
  } else {
    fan_state.power = 0U;
  }
}

void fan_init(void) {
  fan_state.cooldown_counter = current_board->fan_enable_cooldown_time * FAN_TICK_FREQ;
#ifdef STM32F4
  fan_state.stall_threshold = FAN_STALL_THRESHOLD_MIN;
#endif
  llfan_init();
}

// Call this at FAN_TICK_FREQ
void fan_tick(void) {
  if (current_board->has_fan) {
    // Measure fan RPM
    uint16_t fan_rpm_fast = fan_state.tach_counter * (60U * FAN_TICK_FREQ / 4U);   // 4 interrupts per rotation
    fan_state.tach_counter = 0U;
    fan_state.rpm = (fan_rpm_fast + (3U * fan_state.rpm)) / 4U;

    // Cooldown counter to prevent noise on tachometer line.
    if (fan_state.power > 0U) {
      fan_state.cooldown_counter = current_board->fan_enable_cooldown_time * FAN_TICK_FREQ;
    } else {
      if (fan_state.cooldown_counter > 0U) {
        fan_state.cooldown_counter--;
      }
    }

#ifdef STM32F4
    bool fan_stalled = fan_stall_tick(fan_rpm_fast);
    uint8_t start_power = fan_start_tick(fan_stalled);

    // Set PWM and enable line
    pwm_set(TIM3, 3, MAX(fan_state.power, start_power));
    current_board->set_fan_enabled(!fan_stalled && ((fan_state.power > 0U) || (fan_state.cooldown_counter > 0U)));
#else
    // Set PWM and enable line
    pwm_set(TIM3, 3, fan_state.power);
    current_board->set_fan_enabled((fan_state.power > 0U) || (fan_state.cooldown_counter > 0U));
#endif
  }
}
