#!/usr/bin/env python3
"""Flight computer main loop for Eclipse Balloon project."""

import time
import random
import json
import os
import logging
from enum import Enum
from collections import deque
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FlightPhase(Enum):
    """Enumeration of flight phases."""
    GROUND = "GROUND"
    ASCENT_LOW = "ASCENT_LOW"
    ASCENT_HIGH = "ASCENT_HIGH"
    NEAR_SPACE = "NEAR_SPACE"
    DESCENT = "DESCENT"
    LANDED = "LANDED"


@dataclass
class Telemetry:
    """Telemetry data structure."""
    altitude: float  # meters
    temperature: float  # Celsius
    pressure: float  # hPa
    battery_level: float  # percentage (0-100)


@dataclass
class GPS:
    """GPS data structure."""
    latitude: float
    longitude: float
    satellites: int


class SensorManager:
    """Manages sensor data collection."""

    def __init__(self):
        """Initialize sensor manager with mock state."""
        self.altitude = 0.0
        self.temperature = 15.0
        self.pressure = 1013.25
        self.battery_level = 100.0

    def get_telemetry(self) -> Telemetry:
        """
        Get telemetry data from sensors.
        
        Returns:
            Telemetry object with altitude, temperature, pressure, and battery level.
        """
        # Mock altitude: increase by 50-150m per reading (ascent phase)
        # Simulate realistic sensor variations
        altitude_change = random.uniform(-100, 200)
        self.altitude = max(0, self.altitude + altitude_change)

        # Mock temperature: decreases with altitude (~6.5°C per 1000m)
        self.temperature = 15.0 - (self.altitude / 1000.0) * 6.5
        self.temperature += random.uniform(-0.5, 0.5)

        # Mock pressure: decreases exponentially with altitude
        self.pressure = 1013.25 * (1 - self.altitude / 44330) ** 5.255
        self.pressure += random.uniform(-1, 1)

        # Mock battery: slowly drains over flight (~0.1% per reading at 5s intervals)
        self.battery_level = max(0, self.battery_level - random.uniform(0, 0.1))

        return Telemetry(
            altitude=round(self.altitude, 2),
            temperature=round(self.temperature, 2),
            pressure=round(self.pressure, 2),
            battery_level=round(self.battery_level, 2),
        )

    def get_gps(self) -> GPS:
        """
        Get GPS data from receiver.
        
        Returns:
            GPS object with latitude, longitude, and satellite count.
        """
        # Mock GPS: slight drift from launch point (assuming somewhere over continental US)
        latitude = 40.0 + random.uniform(-0.01, 0.01)
        longitude = -105.0 + random.uniform(-0.01, 0.01)
        satellites = random.randint(8, 12)

        return GPS(
            latitude=round(latitude, 6),
            longitude=round(longitude, 6),
            satellites=satellites,
        )


class TelemetryDispatcher:
    """Handles telemetry transmission to API and local logging."""

    def __init__(self, flight_log_file: str = "flight_log.json"):
        """
        Initialize telemetry dispatcher.
        
        Args:
            flight_log_file: Path to local flight log file.
        """
        self.api_url = os.getenv("API_URL")
        self.balloon_id = os.getenv("BALLOON_ID")
        self.flight_log_file = flight_log_file
        self.is_cellular_enabled = True
        self.last_send_time = 0
        self.send_interval = 30  # seconds for ASCENT_HIGH

        if not self.api_url or not self.balloon_id:
            logger.warning(
                "API_URL or BALLOON_ID not set in .env file. "
                "Data transmission will be simulated."
            )

    def send_data(self, telemetry: Telemetry, gps: GPS) -> bool:
        """
        Send telemetry data to API via HTTP POST.
        
        Args:
            telemetry: Telemetry object with sensor data.
            gps: GPS object with location data.
            
        Returns:
            True if successful, False otherwise.
        """
        if not self.is_cellular_enabled:
            logger.debug("Cellular disabled, skipping send")
            return False

        try:
            payload = {
                "balloon_id": self.balloon_id,
                "latitude": gps.latitude,
                "longitude": gps.longitude,
                "altitude": telemetry.altitude,
                "temperature": telemetry.temperature,
                "battery_level": telemetry.battery_level,
            }

            response = requests.post(
                f"{self.api_url}/api/telemetry/receive/",
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info(f"Telemetry sent successfully: {payload}")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send telemetry: {e}")
            return False

    def save_to_log(self, telemetry: Telemetry, gps: GPS) -> None:
        """
        Save telemetry data to local JSON log file.
        
        Args:
            telemetry: Telemetry object with sensor data.
            gps: GPS object with location data.
        """
        payload = {
            "timestamp": time.time(),
            "balloon_id": self.balloon_id,
            "latitude": gps.latitude,
            "longitude": gps.longitude,
            "altitude": telemetry.altitude,
            "temperature": telemetry.temperature,
            "battery_level": telemetry.battery_level,
        }

        try:
            # Load existing log or create new list
            log_data = []
            if os.path.exists(self.flight_log_file):
                with open(self.flight_log_file, "r") as f:
                    log_data = json.load(f)

            log_data.append(payload)

            # Save updated log
            with open(self.flight_log_file, "w") as f:
                json.dump(log_data, f, indent=2)

            logger.debug(f"Telemetry saved to {self.flight_log_file}")
        except IOError as e:
            logger.error(f"Failed to save telemetry to log: {e}")

    def dump_log_to_api(self) -> bool:
        """
        Dump all saved log data to API.
        
        Returns:
            True if all data sent successfully, False otherwise.
        """
        if not os.path.exists(self.flight_log_file):
            logger.info("No flight log to dump")
            return True

        try:
            with open(self.flight_log_file, "r") as f:
                log_data = json.load(f)

            if not log_data:
                return True

            success_count = 0
            for entry in log_data:
                try:
                    response = requests.post(
                        f"{self.api_url}/api/telemetry/receive/",
                        json=entry,
                        timeout=10,
                    )
                    response.raise_for_status()
                    success_count += 1
                except requests.exceptions.RequestException as e:
                    logger.error(f"Failed to dump entry: {e}")

            if success_count == len(log_data):
                # All sent successfully, clear log
                os.remove(self.flight_log_file)
                logger.info(f"Successfully dumped {success_count} entries to API")
                return True
            else:
                logger.warning(
                    f"Dumped {success_count}/{len(log_data)} entries. "
                    "Retained log for later retry."
                )
                return False

        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"Failed to dump log: {e}")
            return False

    def disable_cellular(self) -> None:
        """Disable cellular transmission (mock)."""
        self.is_cellular_enabled = False
        logger.info("Cellular disabled")

    def enable_cellular(self) -> None:
        """Enable cellular transmission (mock)."""
        self.is_cellular_enabled = True
        logger.info("Cellular enabled")


class SafetyManager:
    """Manages safety-critical operations including landing detection and shutdown."""

    def __init__(self, dispatcher: TelemetryDispatcher, check_interval: int = 5):
        """
        Initialize safety manager.
        
        Args:
            dispatcher: TelemetryDispatcher instance for sending final GPS ping.
            check_interval: Sensor check interval in seconds.
        """
        self.dispatcher = dispatcher
        self.check_interval = check_interval
        self.altitude_history = deque(maxlen=10)
        self.is_shutdown = False

    def calculate_descent_rate(self) -> Optional[float]:
        """
        Calculate descent rate based on recent altitude history.
        
        Returns:
            Descent rate in m/s (negative), or None if insufficient data.
        """
        if len(self.altitude_history) < 2:
            return None

        # Calculate rate over last few readings
        alt_diff = self.altitude_history[0] - self.altitude_history[-1]
        time_diff = len(self.altitude_history) * self.check_interval

        if time_diff == 0:
            return None

        return alt_diff / time_diff  # m/s

    def calculate_landing_time(self, current_altitude: float) -> Optional[float]:
        """
        Calculate expected landing time based on descent rate.
        
        Args:
            current_altitude: Current altitude in meters.
            
        Returns:
            Seconds until landing, or None if can't estimate.
        """
        descent_rate = self.calculate_descent_rate()
        
        if descent_rate is None or descent_rate >= 0:
            return None  # Not descending yet or not enough data

        # Estimate time to reach ground (0m)
        # descent_rate is positive (e.g., 5 m/s), altitude is >0
        seconds_to_land = current_altitude / descent_rate

        return max(0, seconds_to_land)

    def check_landing_imminent(self, altitude: float) -> bool:
        """
        Check if landing is imminent (within 60 seconds or < 100m altitude).
        
        Args:
            altitude: Current altitude in meters.
            
        Returns:
            True if landing is imminent, False otherwise.
        """
        # Update altitude history
        self.altitude_history.append(altitude)

        # Condition 1: Altitude below 100m
        if altitude < 100:
            logger.warning("LANDING IMMINENT: Altitude < 100m")
            return True

        # Condition 2: Within 60 seconds of landing
        landing_time = self.calculate_landing_time(altitude)
        if landing_time is not None and landing_time <= 60:
            logger.warning(f"LANDING IMMINENT: ETA {landing_time:.1f} seconds")
            return True

        return False

    def send_gps_ping(self, gps: GPS) -> bool:
        """
        Send final landing GPS coordinates to API.
        
        Args:
            gps: GPS object with landing location.
            
        Returns:
            True if successful, False otherwise.
        """
        try:
            payload = {
                "balloon_id": self.dispatcher.balloon_id,
                "latitude": gps.latitude,
                "longitude": gps.longitude,
                "altitude": 0.0,  # Landed
                "temperature": 0.0,
                "battery_level": 0.0,
                "event_type": "LANDING",
            }

            response = requests.post(
                f"{self.dispatcher.api_url}/api/telemetry/receive/",
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info(f"Final GPS PING sent: {payload}")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send final GPS ping: {e}")
            return False

    def graceful_shutdown(self, gps: GPS) -> None:
        """
        Execute graceful shutdown sequence.
        
        Closes file handles, sends final GPS ping, and halts the system.
        
        Args:
            gps: GPS object with landing location.
        """
        if self.is_shutdown:
            logger.warning("Shutdown already in progress")
            return

        self.is_shutdown = True
        logger.critical("=" * 70)
        logger.critical("INITIATING GRACEFUL SHUTDOWN")
        logger.critical("=" * 70)

        # Step 1: Close file handles (flight log)
        try:
            if hasattr(self.dispatcher, 'flight_log_file'):
                if os.path.exists(self.dispatcher.flight_log_file):
                    logger.info(f"Closing file handle: {self.dispatcher.flight_log_file}")
                    # File is closed automatically, but ensure it's flushed
                    # In production, would ensure all file handles are closed
        except Exception as e:
            logger.error(f"Error closing file handles: {e}")

        # Step 2: Send final GPS PING to API
        try:
            logger.info("Sending final GPS PING to mission control...")
            self.send_gps_ping(gps)
        except Exception as e:
            logger.error(f"Error sending final GPS ping: {e}")

        # Step 3: Execute system halt (mocked)
        logger.critical("Executing system halt command...")
        logger.critical("[MOCK] sudo halt -p (Power off)")
        logger.critical("System will shut down in 5 seconds...")
        time.sleep(5)
        logger.critical("BALLOON LANDED AND SHUT DOWN SUCCESSFULLY")


class FlightComputer:
    """Main flight computer logic."""

    def __init__(self, descent_threshold: int = 3):
        """
        Initialize flight computer.
        
        Args:
            descent_threshold: Number of consecutive readings to trigger descent phase.
        """
        self.sensor_manager = SensorManager()
        self.dispatcher = TelemetryDispatcher()
        self.safety_manager = SafetyManager(self.dispatcher)
        self.current_phase = FlightPhase.GROUND
        self.altitude_history = deque(maxlen=descent_threshold)
        self.descent_threshold = descent_threshold

    def update_phase(self, altitude: float) -> FlightPhase:
        """
        Update flight phase based on altitude and descent detection.
        
        Args:
            altitude: Current altitude in meters.
            
        Returns:
            Updated flight phase.
        """
        self.altitude_history.append(altitude)

        # Check for descent: 3 consecutive readings with decreasing altitude
        in_descent = (
            len(self.altitude_history) == self.descent_threshold
            and all(
                self.altitude_history[i] > self.altitude_history[i + 1]
                for i in range(len(self.altitude_history) - 1)
            )
        )

        if in_descent and self.current_phase not in (FlightPhase.DESCENT, FlightPhase.LANDED):
            self.current_phase = FlightPhase.DESCENT
        elif self.current_phase == FlightPhase.DESCENT and altitude < 100:
            self.current_phase = FlightPhase.LANDED
        elif self.current_phase in (FlightPhase.GROUND, FlightPhase.ASCENT_LOW, FlightPhase.ASCENT_HIGH):
            # Determine ascent phase
            if altitude < 1000:
                self.current_phase = FlightPhase.ASCENT_LOW
            elif altitude < 24000:
                self.current_phase = FlightPhase.ASCENT_HIGH
            else:
                self.current_phase = FlightPhase.NEAR_SPACE

        return self.current_phase

    def run(self, duration: int = 3600, check_interval: int = 5):
        """
        Run the main flight loop.
        
        Args:
            duration: Total duration to run in seconds.
            check_interval: Interval between sensor checks in seconds.
        """
        print("=" * 70)
        print("Eclipse Balloon Flight Computer Starting")
        print("=" * 70)
        print()

        start_time = time.time()
        iteration = 0
        last_phase = None

        try:
            while time.time() - start_time < duration:
                iteration += 1
                current_time = time.time()

                # Get sensor data
                telemetry = self.sensor_manager.get_telemetry()
                gps = self.sensor_manager.get_gps()

                # Update flight phase
                phase = self.update_phase(telemetry.altitude)

                # Handle phase transitions
                if phase != last_phase:
                    logger.info(f"Phase transition: {last_phase} -> {phase.value}")
                    
                    # NEAR_SPACE: disable cellular and begin logging
                    if phase == FlightPhase.NEAR_SPACE:
                        self.dispatcher.disable_cellular()
                    
                    # DESCENT: reconnect and dump log
                    elif phase == FlightPhase.DESCENT:
                        self.dispatcher.enable_cellular()
                        logger.info("Attempting to dump flight log to API...")
                        self.dispatcher.dump_log_to_api()
                    
                    last_phase = phase

                # Phase-specific telemetry handling
                if phase == FlightPhase.ASCENT_HIGH:
                    # Send data every 30 seconds
                    if current_time - self.dispatcher.last_send_time >= self.dispatcher.send_interval:
                        self.dispatcher.send_data(telemetry, gps)
                        self.dispatcher.last_send_time = current_time

                elif phase == FlightPhase.NEAR_SPACE:
                    # Save to local log (offline mode)
                    self.dispatcher.save_to_log(telemetry, gps)

                elif phase == FlightPhase.DESCENT:
                    # Attempt to send in real-time
                    self.dispatcher.send_data(telemetry, gps)

                # Safety check: Monitor for landing
                if phase == FlightPhase.DESCENT:
                    if self.safety_manager.check_landing_imminent(telemetry.altitude):
                        # Print final status before shutdown
                        elapsed = int(time.time() - start_time)
                        print(f"[{elapsed:04d}s] Iteration {iteration}")
                        print(f"  Phase: {phase.value}")
                        print(f"  Altitude: {telemetry.altitude:8.2f} m")
                        print(f"  Temp: {telemetry.temperature:6.2f} °C")
                        print(f"  Pressure: {telemetry.pressure:8.2f} hPa")
                        print(f"  Battery: {telemetry.battery_level:5.1f} %")
                        print(f"  GPS: ({gps.latitude:.6f}, {gps.longitude:.6f}) | Sats: {gps.satellites}")
                        landing_time = self.safety_manager.calculate_landing_time(telemetry.altitude)
                        if landing_time is not None:
                            print(f"  ETA Landing: {landing_time:.1f} seconds")
                        print()
                        
                        # Trigger graceful shutdown
                        self.safety_manager.graceful_shutdown(gps)
                        break  # Exit main loop

                # Print status
                elapsed = int(time.time() - start_time)
                print(f"[{elapsed:04d}s] Iteration {iteration}")
                print(f"  Phase: {phase.value}")
                print(f"  Altitude: {telemetry.altitude:8.2f} m")
                print(f"  Temp: {telemetry.temperature:6.2f} °C")
                print(f"  Pressure: {telemetry.pressure:8.2f} hPa")
                print(f"  Battery: {telemetry.battery_level:5.1f} %")
                print(f"  GPS: ({gps.latitude:.6f}, {gps.longitude:.6f}) | Sats: {gps.satellites}")
                print(f"  Cellular: {'ENABLED' if self.dispatcher.is_cellular_enabled else 'DISABLED'}")
                
                # Display landing estimate if descending
                if phase == FlightPhase.DESCENT:
                    landing_time = self.safety_manager.calculate_landing_time(telemetry.altitude)
                    if landing_time is not None:
                        print(f"  ETA Landing: {landing_time:.1f} seconds")
                
                print()

                # Sleep until next check
                time.sleep(check_interval)

        except KeyboardInterrupt:
            print("\n" + "=" * 70)
            print("Flight loop interrupted by user")
            print("=" * 70)


if __name__ == "__main__":
    flight_computer = FlightComputer()
    flight_computer.run()
