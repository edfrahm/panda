#pragma once

// The comma 3's panda (dos, STM32F413) talks to the SoM over USB only, and has
// no sound codec, microphone or digital temperature sensor. These stand in for
// the STM32H7 drivers so the shared code builds unchanged.

// ******************** spi ********************
uint16_t spi_error_count = 0U;

void spi_init(void) {
}

void can_tx_comms_resume_spi(void) {
}

#ifndef BOOTSTUB
// ******************** sound ********************
uint16_t sound_output_level = 0U;

void sound_tick(void) {
}

// ******************** digital temperature sensor ********************
void dts_init(void) {
}

// -40C is the bottom of the health packet range, so the temperature field reads 0
float dts_get_temperature(void) {
  return -40.0f;
}
#endif
