#!/usr/bin/env python3
"""Flight computer main loop for Eclipse Balloon project."""

import time
import random
import json
import os
import argparse
import logging
import urllib.parse
import subprocess
import threading
from enum import Enum
from collections import deque
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
try:
    import gpsd
except ImportError:
    gpsd = None

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
    altitude: float
    satellites: int


class SensorManager:
    """Manages sensor data collection."""

    def __init__(self):
        """Initialize sensor manager with mock state."""
        self.altitude = 0.0
        self.temperature = 15.0
        self.pressure = 1013.25
        self.battery_level = 100.0
        self.use_real_gps = os.getenv("USE_REAL_GPS", "false").lower() == "true"
        self.last_known_gps = None
        self.gpsd_connected = False
        if self.use_real_gps and gpsd:
            try:
                gpsd.connect()
                self.gpsd_connected = True
            except Exception as e:
                logger.warning(f"Failed to connect to gpsd: {e}")

    def get_telemetry(self) -> Telemetry:
        """
        Get telemetry data from sensors.
        
        Returns:
            Telemetry object with altitude, temperature, pressure, and battery level.
        """
        # Altitude: use GPS if real, else mock
        if not self.use_real_gps:
            # Mock altitude: consistently increase during ascent phase
            # Simulate realistic sensor variations
            altitude_change = random.uniform(50, 200)
            self.altitude = self.altitude + altitude_change

        # Mock temperature: decreases with altitude (~6.5°C per 1000m) - since sensor not working
        self.temperature = 15.0 - (self.altitude / 1000.0) * 6.5
        self.temperature = max(-273.15, self.temperature)
        self.temperature += random.uniform(-0.5, 0.5)

        # Mock pressure: decreases exponentially with altitude
        self.pressure = 1013.25 * max(0.0, 1 - self.altitude / 44330) ** 5.255
        self.pressure += random.uniform(-1, 1)
        self.pressure = max(0.0, self.pressure)

        # Mock battery: slowly drains over flight (~0.1% per reading at 5s intervals)
        self.battery_level = max(0, self.battery_level - random.uniform(0, 0.1))

        if self.battery_level < 5.0:
            logger.critical("BATTERY CRITICALLY LOW (<5%). INITIATING EMERGENCY SHUTDOWN.")
            subprocess.run(["sudo", "halt", "-p"], check=False)

        return Telemetry(
            altitude=round(self.altitude, 2),
            temperature=round(self.temperature, 2),
            pressure=round(self.pressure, 2),
            battery_level=round(self.battery_level, 2),
        )

    def _read_sixfab_nmea(self) -> Optional[GPS]:
        """Fallback to directly reading NMEA from Sixfab ttyUSB1."""
        if not os.path.exists('/dev/ttyUSB1'):
            return None
        try:
            # Read a GGA line with timeout. GGA contains altitude, sats, and fix quality.
            cmd = "timeout 2 cat /dev/ttyUSB1 | grep -E '\\$GPGGA|\\$GNGGA' | head -n 1"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                line = result.stdout.strip()
                parts = line.split(',')
                if len(parts) >= 10 and parts[6] != '0':  # 0 means invalid fix
                    lat_raw, lat_dir = parts[2], parts[3]
                    lon_raw, lon_dir = parts[4], parts[5]
                    sats, alt_raw = parts[7], parts[9]
                    
                    if lat_raw and lon_raw and alt_raw:
                        lat = float(lat_raw[:2]) + float(lat_raw[2:])/60.0
                        if lat_dir == 'S': lat = -lat
                        
                        lon = float(lon_raw[:3]) + float(lon_raw[3:])/60.0
                        if lon_dir == 'W': lon = -lon
                        
                        return GPS(
                            latitude=round(lat, 6),
                            longitude=round(lon, 6),
                            altitude=round(float(alt_raw), 2),
                            satellites=int(sats)
                        )
        except Exception as e:
            logger.debug(f"Direct NMEA read failed: {e}")
        return None

    def get_gps(self) -> GPS:
        """
        Get GPS data from receiver.
        
        Returns:
            GPS object with latitude, longitude, and satellite count.
        """
        if self.use_real_gps:
            # Try to connect to gpsd if not connected
            if not self.gpsd_connected and gpsd:
                try:
                    gpsd.connect()
                    self.gpsd_connected = True
                except Exception:
                    pass

            if self.gpsd_connected:
                try:
                    packet = gpsd.get_current()
                    if getattr(packet, 'mode', 0) >= 2:
                        latitude = getattr(packet, 'lat', 0.0)
                        longitude = getattr(packet, 'lon', 0.0)
                        altitude = getattr(packet, 'alt', 0.0)
                        satellites = getattr(packet, 'sats', 0)
                        if altitude:
                            self.altitude = altitude
                        
                        current_gps = GPS(
                            latitude=round(latitude, 6),
                            longitude=round(longitude, 6),
                            altitude=round(altitude, 2),
                            satellites=satellites,
                        )
                        self.last_known_gps = current_gps
                        return current_gps
                    else:
                        logger.info("Waiting for GPS Fix (gpsd)...")
                except Exception as e:
                    if "NoFixError" not in str(type(e)):
                        logger.warning(f"Failed to read from gpsd: {e}")
            else:
                # Fallback: Read directly from Sixfab NMEA port
                direct_gps = self._read_sixfab_nmea()
                if direct_gps:
                    self.altitude = direct_gps.altitude
                    self.last_known_gps = direct_gps
                    return direct_gps
                else:
                    logger.info("Waiting for GPS Fix (NMEA fallback)...")
            
            if self.last_known_gps:
                return self.last_known_gps
            
            return GPS(latitude=0.0, longitude=0.0, altitude=0.0, satellites=0)
        
        # Mock GPS: slight drift from launch point (assuming somewhere over Madrid, Spain)
        latitude = 40.4168 + random.uniform(-0.01, 0.01)
        longitude = -3.7038 + random.uniform(-0.01, 0.01)
        satellites = random.randint(8, 12)
        altitude = self.altitude  # Use the mock altitude

        return GPS(
            latitude=round(latitude, 6),
            longitude=round(longitude, 6),
            altitude=round(altitude, 2),
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
        self.send_interval = 5  # seconds for mocked telemetry loop
        self.debug = os.getenv("DEBUG", "false").lower() == "true"

        if not self.api_url or not self.balloon_id:
            logger.warning(
                "API_URL or BALLOON_ID not set in .env file. "
                "Data transmission will be simulated."
            )

    def _resolve_url(self, path: str) -> str:
        """Resolve the final API endpoint URL safely."""
        if not self.api_url:
            return path
        return urllib.parse.urljoin(self.api_url.rstrip('/') + '/', path.lstrip('/'))

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

            url = self._resolve_url("/api/telemetry/receive/")
            
            if True:
                print("=" * 70)
                print("DEBUG: API REQUEST")
                print("=" * 70)
                print(f"URL: {url}")
                print(f"Method: POST")
                print(f"Headers: {{'Content-Type': 'application/json'}}")
                print(f"Payload: {json.dumps(payload, indent=2)}")
                print()

            response = requests.post(
                url,
                json=payload,
                timeout=10,
            )
            
            if True:
                print("=" * 70)
                print("DEBUG: API RESPONSE")
                print("=" * 70)
                print(f"Status Code: {response.status_code}")
                print(f"Response Headers: {dict(response.headers)}")
                print(f"Response Body: {response.text}")
                print()

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
                    url = self._resolve_url("/api/telemetry/receive/")
                    print("=" * 70)
                    print("DEBUG: API REQUEST (DUMP LOG)")
                    print("=" * 70)
                    print(f"URL: {url}")
                    print(f"Method: POST")
                    print(f"Headers: {{'Content-Type': 'application/json'}}")
                    print(f"Payload: {json.dumps(entry, indent=2)}")
                    print()
                    
                    response = requests.post(
                        url,
                        json=entry,
                        timeout=10,
                    )
                    
                    print("=" * 70)
                    print("DEBUG: API RESPONSE (DUMP LOG)")
                    print("=" * 70)
                    print(f"Status Code: {response.status_code}")
                    print(f"Response Headers: {dict(response.headers)}")
                    print(f"Response Body: {response.text}")
                    print()
                    
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
            Descent rate in m/s (positive when descending), or None if insufficient data.
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
        
        if descent_rate is None or descent_rate <= 0:
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

            url = self.dispatcher._resolve_url("/api/telemetry/receive/")
            print("=" * 70)
            print("DEBUG: API REQUEST (GPS PING)")
            print("=" * 70)
            print(f"URL: {url}")
            print(f"Method: POST")
            print(f"Headers: {{'Content-Type': 'application/json'}}")
            print(f"Payload: {json.dumps(payload, indent=2)}")
            print()

            response = requests.post(
                url,
                json=payload,
                timeout=10,
            )

            print("=" * 70)
            print("DEBUG: API RESPONSE (GPS PING)")
            print("=" * 70)
            print(f"Status Code: {response.status_code}")
            print(f"Response Headers: {dict(response.headers)}")
            print(f"Response Body: {response.text}")
            print()

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

        # Step 3: Execute system halt
        logger.critical("Executing system halt command...")
        subprocess.run(["sudo", "halt", "-p"], check=False)
        logger.critical("System will shut down in 5 seconds...")
        time.sleep(5)
        logger.critical("BALLOON LANDED AND SHUT DOWN SUCCESSFULLY")


class HardwareManager:
    """Manages hardware-specific power and modems."""

    def __init__(self, no_wifi: bool = False):
        self.no_wifi = no_wifi

    def power_save(self) -> None:
        """Run tvservice -o to disable HDMI and save battery."""
        try:
            logger.info("Disabling HDMI to save power...")
            subprocess.run(["tvservice", "-o"], check=False)
        except Exception as e:
            logger.error(f"Failed to disable HDMI: {e}")

    def wake_modem(self) -> None:
        """Pulse GPIO 22 using pinctrl to wake the Sixfab HAT."""
        try:
            logger.info("Waking up modem on GPIO 22...")
            subprocess.run(["pinctrl", "set", "22", "op", "dh"], check=False)
            time.sleep(2)
            subprocess.run(["pinctrl", "set", "22", "op", "dl"], check=False)
            
            logger.info("Sending AT+QGPS=1 to power on GPS engine...")
            subprocess.run("echo -e 'AT+QGPS=1\\r' > /dev/ttyUSB2", shell=True, check=False)
        except Exception as e:
            logger.error(f"Failed to wake modem: {e}")

    def shutdown_system(self) -> None:
        """Run sudo halt -p for the final fire-safety shutdown."""
        try:
            logger.critical("Executing system halt command (sudo halt -p)...")
            subprocess.run(["sudo", "halt", "-p"], check=False)
        except Exception as e:
            logger.error(f"Failed to execute shutdown: {e}")

    def manage_network(self) -> None:
        """Configure usb0 interface and routing."""
        try:
            if self.no_wifi:
                logger.info("Disabling default route on wlan0 to force cellular testing...")
                subprocess.run(["sudo", "ip", "route", "del", "default", "dev", "wlan0"], check=False)

            if os.path.exists("/sys/class/net/usb0"):
                logger.info("Found usb0 interface, configuring network...")
                subprocess.run(["sudo", "ip", "link", "set", "usb0", "up"], check=False)
                subprocess.run(["sudo", "dhclient", "usb0"], check=False)
                subprocess.run(["sudo", "ip", "route", "add", "default", "via", "192.168.225.1", "dev", "usb0", "metric", "800"], check=False)
                
                logger.info("--- Cellular Debugging Info ---")
                usb0_addr = subprocess.run(["ip", "addr", "show", "usb0"], capture_output=True, text=True)
                logger.info(f"usb0 IP Configuration:\\n{usb0_addr.stdout}")
                routes = subprocess.run(["ip", "route"], capture_output=True, text=True)
                logger.info(f"Routing Table:\\n{routes.stdout}")
                mmcli = subprocess.run(["mmcli", "-m", "any"], capture_output=True, text=True)
                logger.info(f"Modem Status:\\n{mmcli.stdout}")
                lsusb = subprocess.run(["lsusb"], capture_output=True, text=True)
                logger.info(f"USB Devices:\\n{lsusb.stdout}")
                logger.info("-------------------------------")
            else:
                logger.warning("usb0 interface not found, skipping network configuration.")
                logger.info("--- Cellular Debugging Info (Missing usb0) ---")
                lsusb = subprocess.run(["lsusb"], capture_output=True, text=True)
                logger.info(f"USB Devices:\\n{lsusb.stdout}")
                mmcli = subprocess.run(["mmcli", "-m", "any"], capture_output=True, text=True)
                logger.info(f"Modem Status:\\n{mmcli.stdout}")
                logger.info("----------------------------------------------")
        except Exception as e:
            logger.error(f"Failed to manage network: {e}")


class NetworkHealer(threading.Thread):
    """Background thread to monitor and heal cellular connection."""

    def __init__(self, hardware_manager: HardwareManager, sensor_manager: SensorManager):
        super().__init__(daemon=True)
        self.hardware = hardware_manager
        self.sensors = sensor_manager
        self.running = True
        self.healing_paused = False
        self.check_interval = 10  # seconds
        self.consecutive_failures = 0

    def check_connection(self) -> bool:
        """Ping 8.8.8.8 to verify connectivity."""
        try:
            # -c 1 = 1 packet, -W 5 = 5 sec timeout
            result = subprocess.run(["ping", "-c", "1", "-W", "5", "8.8.8.8"], capture_output=True)
            return result.returncode == 0
        except Exception:
            return False

    def get_public_ip(self) -> str:
        try:
            result = subprocess.run(["curl", "-s", "--connect-timeout", "5", "https://api.ipify.org"], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
        return "Unknown"

    def get_tailscale_ip(self) -> str:
        try:
            result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "Unknown"

    def notify_webhook(self, public_ip: str, tailscale_ip: str) -> None:
        webhook_url = os.getenv("WEBHOOK_URL")
        if not webhook_url:
            return
        try:
            payload = {
                "text": f"🚀 Eclipse Balloon Connection Re-established!\\n**Public IP:** {public_ip}\\n**Tailscale IP:** {tailscale_ip}"
            }
            requests.post(webhook_url, json=payload, timeout=5)
            logger.info("Sent webhook notification.")
        except Exception as e:
            logger.error(f"Failed to send webhook: {e}")

    def heal_connection(self) -> bool:
        logger.warning(f"Connection lost. Failure {self.consecutive_failures}. Attempting to heal...")
        
        # Level 1: Manage network (ip link, dhclient, routes)
        self.hardware.manage_network()
        if self.check_connection():
            return True
            
        # Level 2: mmcli soft reset (dynamic targeting)
        if self.consecutive_failures >= 2:
            logger.warning("Level 2 Healing: Soft resetting modem via mmcli -m any")
            subprocess.run(["mmcli", "-m", "any", "--reset"], check=False)
            time.sleep(10)  # Give modem time to restart
            self.hardware.manage_network()
            if self.check_connection():
                return True
                
        # Level 3: Physical GPIO power cycle
        if self.consecutive_failures >= 4:
            logger.critical("Level 3 Healing: Hard power cycle via GPIO 22")
            self.hardware.wake_modem()  # Pulses GPIO 22
            time.sleep(15)  # Give modem time to boot
            self.hardware.manage_network()
            if self.check_connection():
                return True
                
        return False

    def run(self) -> None:
        was_connected = True
        
        while self.running:
            try:
                # Use current altitude
                altitude = self.sensors.altitude
                
                # Altitude-Aware Connectivity
                if not self.healing_paused and altitude > 15000:
                    logger.info("Altitude > 15000m. Pausing connection healing to save battery.")
                    self.healing_paused = True
                elif self.healing_paused and altitude < 12000:
                    logger.info("Altitude < 12000m. Resuming connection healing.")
                    self.healing_paused = False
                    
                if self.healing_paused:
                    time.sleep(self.check_interval)
                    continue
                    
                # Low-Level Pings
                is_connected = self.check_connection()
                
                if is_connected:
                    if not was_connected:
                        logger.info("Connection re-established!")
                        public_ip = self.get_public_ip()
                        tailscale_ip = self.get_tailscale_ip()
                        self.notify_webhook(public_ip, tailscale_ip)
                    self.consecutive_failures = 0
                    was_connected = True
                else:
                    was_connected = False
                    self.consecutive_failures += 1
                    healed = self.heal_connection()
                    if healed:
                        logger.info("Connection successfully healed!")
                        public_ip = self.get_public_ip()
                        tailscale_ip = self.get_tailscale_ip()
                        self.notify_webhook(public_ip, tailscale_ip)
                        self.consecutive_failures = 0
                        was_connected = True

            except Exception as e:
                logger.error(f"Error in NetworkHealer: {e}")
                
            time.sleep(self.check_interval)


class FlightComputer:
    """Main flight computer logic."""

    def __init__(self, descent_threshold: int = 3, no_wifi: bool = False):
        """
        Initialize flight computer.
        
        Args:
            descent_threshold: Number of consecutive readings to trigger descent phase.
            no_wifi: Stop using WiFi for internet to force cellular connection testing.
        """
        self.sensor_manager = SensorManager()
        self.dispatcher = TelemetryDispatcher()
        self.safety_manager = SafetyManager(self.dispatcher)
        self.hardware_manager = HardwareManager(no_wifi=no_wifi)
        self.network_healer = NetworkHealer(self.hardware_manager, self.sensor_manager)
        self.current_phase = FlightPhase.GROUND
        self.altitude_history = deque(maxlen=descent_threshold)
        self.descent_threshold = descent_threshold
        self.max_altitude = 0.0

    def update_phase(self, altitude: float) -> FlightPhase:
        """
        Update flight phase based on altitude and descent detection.
        
        Args:
            altitude: Current altitude in meters.
            
        Returns:
            Updated flight phase.
        """
        self.max_altitude = max(self.max_altitude, altitude)
        self.altitude_history.append(altitude)

        # Check for descent: 3 consecutive readings with decreasing altitude, and only if max_altitude > 4000m
        in_descent = (
            len(self.altitude_history) == self.descent_threshold
            and all(
                self.altitude_history[i] > self.altitude_history[i + 1]
                for i in range(len(self.altitude_history) - 1)
            )
            and self.max_altitude > 4000
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
        
        # Initial hardware setup
        self.hardware_manager.wake_modem()
        self.hardware_manager.manage_network()

        start_time = time.time()
        iteration = 0
        last_phase = None
        last_network_manage_time = 0

        self.network_healer.start()

        try:
            while time.time() - start_time < duration:
                iteration += 1
                current_time = time.time()

                # Get sensor data
                gps = self.sensor_manager.get_gps()
                telemetry = self.sensor_manager.get_telemetry()

                # Update flight phase
                phase = self.update_phase(telemetry.altitude)

                # Handle phase transitions
                if phase != last_phase:
                    logger.info(f"Phase transition: {last_phase} -> {phase.value}")
                    
                    # GROUND phase setup (also handled at startup)
                    if phase == FlightPhase.GROUND:
                        self.hardware_manager.wake_modem()
                        self.hardware_manager.manage_network()
                    
                    # NEAR_SPACE: disable cellular and begin logging
                    if phase == FlightPhase.NEAR_SPACE:
                        self.dispatcher.disable_cellular()
                    
                    # DESCENT or LANDED: reconnect and dump log
                    elif phase in (FlightPhase.DESCENT, FlightPhase.LANDED):
                        self.dispatcher.enable_cellular()
                        logger.info("Attempting to dump flight log to API in background...")
                        threading.Thread(target=self.dispatcher.dump_log_to_api, daemon=True).start()
                    
                    last_phase = phase

                # Phase-specific telemetry handling
                if phase in (FlightPhase.ASCENT_LOW, FlightPhase.ASCENT_HIGH):
                    # Send mocked telemetry every loop interval (every 5 seconds)
                    if current_time - self.dispatcher.last_send_time >= self.dispatcher.send_interval:
                        self.dispatcher.send_data(telemetry, gps)
                        self.dispatcher.last_send_time = current_time

                elif phase == FlightPhase.NEAR_SPACE:
                    # Save to local log (offline mode)
                    self.dispatcher.save_to_log(telemetry, gps)

                elif phase in (FlightPhase.DESCENT, FlightPhase.LANDED):
                    # Attempt to send in real-time
                    self.dispatcher.send_data(telemetry, gps)
                    
                    # Manage network every 60 seconds to grab a tower IP
                    if current_time - last_network_manage_time >= 60:
                        self.hardware_manager.manage_network()
                        last_network_manage_time = current_time

                # Safety check: Monitor for landing
                if phase in (FlightPhase.DESCENT, FlightPhase.LANDED):
                    if phase == FlightPhase.LANDED or self.safety_manager.check_landing_imminent(telemetry.altitude):
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
        finally:
            self.network_healer.running = False
            self.network_healer.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Eclipse Balloon Flight Computer")
    parser.add_argument("--name", type=str, help="Override the balloon ID/name from .env")
    parser.add_argument("--no-wifi", action="store_true", help="Stop using WiFi for internet (removes wlan0 default route)")
    args = parser.parse_args()

    if args.name:
        os.environ["BALLOON_ID"] = args.name

    flight_computer = FlightComputer(no_wifi=args.no_wifi)
    flight_computer.run()
