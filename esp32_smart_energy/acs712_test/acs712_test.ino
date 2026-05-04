#define CURRENT_PIN      35
#define ACS712_30A       0.066
#define ACS712_20A       0.100
#define ACS712_5A        0.185

#define SENSITIVITY      ACS712_30A

#define ADC_MAX          4095.0
#define VREF_MV          3300.0
#define SAMPLE_MS        200

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("=== ACS712 Test (Pin 35) ===");
  Serial.println("Waiting 2s for sensor to stabilize...");
  delay(2000);
}

void loop() {
  double sum   = 0.0;
  double sumSq = 0.0;
  long   n     = 0;
  unsigned long start = millis();

  while (millis() - start < SAMPLE_MS) {
    double sample  = analogRead(CURRENT_PIN);
    sum      += sample;
    sumSq    += sample * sample;
    n++;
  }

  double mean     = sum / n;
  double variance = (sumSq / n) - (mean * mean);
  if (variance < 0) variance = 0;

  double rmsADC = sqrt(variance);
  double rmsMV  = (rmsADC / ADC_MAX) * VREF_MV;
  double amps   = rmsMV / (SENSITIVITY * 1000.0);

  Serial.printf("Mean ADC: %6.1f | RMS ADC: %6.3f | RMS mV: %6.2f | Current: %.4f A\n",
                mean, rmsADC, rmsMV, amps);

  delay(500);
}
