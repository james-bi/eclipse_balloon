from flight_loop import SensorManager

sm = SensorManager()
sm.altitude = 45000.0 # Above the 44330 threshold

try:
    telemetry = sm.get_telemetry()
    print("Telemetry success:", telemetry)
except Exception as e:
    print("Failed!", e)
