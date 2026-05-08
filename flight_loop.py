#!/usr/bin/env python3
"""Flight computer main loop for Eclipse Balloon project."""

import time
import random
import json
import os
import io
import argparse
import logging
import urllib.parse
import subprocess
import threading
import sys
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

try:
    from picamera2 import Picamera2
    picamera_available = True
except ImportError:
    picamera_available = False

try:
    from PIL import Image, ImageDraw
    pil_available = True
except ImportError:
    pil_available = False

import boto3

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
        self.simulating_descent = False
        self.last_known_gps = None
        self.gpsd_connected = False
        if self.use_real_gps and gpsd:
            try:
                gpsd.connect()
                self.gpsd_connected = True
            except Exception as e:
                logger.warning(f"Failed to connect to gpsd: {e}")

    def start_descent_simulation(self):
        """For mock flights, force the altitude to start decreasing."""
        logger.info("MOCK: Starting simulated descent.")
        self.simulating_descent = True

    def get_telemetry(self) -> Telemetry:
        """
        Get telemetry data from sensors.
        
        Returns:
            Telemetry object with altitude, temperature, pressure, and battery level.
        """
        # Altitude: use GPS if real, else mock
        if not self.use_real_gps:
            # Mock altitude: consistently increase during ascent phase
            if self.simulating_descent:
                # Simulate descent
                altitude_change = random.uniform(-200, -50)
            else:
                # Simulate ascent
                altitude_change = random.uniform(50, 200)
            self.altitude = self.altitude + altitude_change
            self.altitude = max(0, self.altitude)  # Don't go below ground

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

    def _read_quectel_gps(self) -> Optional[GPS]:
        """Fetches and parses GPS data from the Quectel modem."""
        print("🛰️ Checking satellite link...")
        try:
            # Requesting location data (Format 2)
            # Output format: <UTC>,<lat>,<lon>,<hdop>,<alt>,<fix>,<cog>,<spkm>,<spkn>,<date>,<nsat>
            raw_output = run_modem_cmd('AT+QGPSLOC=2')

            if "+QGPSLOC:" in raw_output:
                # Clean the string to get just the numbers
                data_string = raw_output.split("+QGPSLOC: ")[1]
                parts = data_string.split(",")
                
                lat = float(parts[1])
                lon = float(parts[2])
                alt = float(parts[4])
                sats_str = parts[10].replace('OK', '').strip()
                sats = int(sats_str)
                
                print(" ✅ FIX ACQUIRED!")
                print(f" 📍 Position: {lat}, {lon}")
                print(f" 🏔️ Altitude: {alt} meters MSL")
                print(f" 📡 Satellites in view: {sats}")
                
                return GPS(
                    latitude=round(lat, 6),
                    longitude=round(lon, 6),
                    altitude=round(alt, 2),
                    satellites=sats
                )

            elif "516" in raw_output or ("ERROR" in raw_output and "505" not in raw_output):
                print(" ❌ STATUS: NO FIX.")
                print("    Reason: The antenna can't see enough satellites yet.")
                print("    Action: Move the sticker antenna outside or away from buildings.")
                
            elif "505" in raw_output:
                print(" ❌ STATUS: GPS ENGINE OFF.")
                print("    Action: Run the 'initialize_flight_gps' script first!")
                
            else:
                print(f" ❓ UNKNOWN ERROR: {raw_output}")

        except Exception as e:
            print(f" 🛠️ HARDWARE ERROR: Could not talk to the modem. ({str(e)})")
            print("    Check: Is the Sixfab HAT securely attached?")

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
                # Fallback: Read directly from Quectel modem
                direct_gps = self._read_quectel_gps()
                if direct_gps:
                    self.altitude = direct_gps.altitude
                    self.last_known_gps = direct_gps
                    return direct_gps
                else:
                    logger.info("Waiting for GPS Fix (Quectel fallback)...")
            
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

    def send_data(self, telemetry: Telemetry, gps: GPS, flight_phase: FlightPhase = FlightPhase.GROUND) -> bool:
        """
        Send telemetry data to API via HTTP POST.
        
        Args:
            telemetry: Telemetry object with sensor data.
            gps: GPS object with location data.
            flight_phase: Current flight phase.
            
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
                "flight_phase": flight_phase.value,
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

    def save_to_log(self, telemetry: Telemetry, gps: GPS, flight_phase: FlightPhase = FlightPhase.GROUND) -> None:
        """
        Save telemetry data to local JSON log file.
        
        Args:
            telemetry: Telemetry object with sensor data.
            gps: GPS object with location data.
            flight_phase: Current flight phase.
        """
        payload = {
            "timestamp": time.time(),
            "balloon_id": self.balloon_id,
            "latitude": gps.latitude,
            "longitude": gps.longitude,
            "altitude": telemetry.altitude,
            "temperature": telemetry.temperature,
            "battery_level": telemetry.battery_level,
            "flight_phase": flight_phase.value,
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
        """Disable cellular transmission and network radios to save battery."""
        self.is_cellular_enabled = False
        logger.info("💤 SLEEPING (Power Save) NETWORK RADIOS")
        try:
            logger.info("Turning Wi-Fi off...")
            subprocess.run("sudo nmcli radio wifi off", shell=True, check=True)
            logger.info("Turning Cellular off...")
            subprocess.run("sudo nmcli radio wwan off", shell=True, check=True)
            logger.info("Radios disabled. Battery consumption reduced.")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to toggle radios: {e}")
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")

    def enable_cellular(self) -> None:
        """Enable cellular transmission and network radios."""
        self.is_cellular_enabled = True
        logger.info("🚀 ACTIVATING NETWORK RADIOS")
        try:
            logger.info("Turning Wi-Fi on...")
            subprocess.run("sudo nmcli radio wifi on", shell=True, check=True)
            logger.info("Turning Cellular on...")
            subprocess.run("sudo nmcli radio wwan on", shell=True, check=True)
            logger.info("Radios enabled. Waiting for reconnect...")
            time.sleep(5)
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to toggle radios: {e}")
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")


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
                "flight_phase": FlightPhase.LANDED.value,
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


class CameraManager:
    """Manages camera operations, photo capture, S3 uploads, and storage management."""

    def __init__(self, flight_name: str, bucket_name: str, api_url: str, dispatcher: TelemetryDispatcher):
        """
        Initialize camera manager.
        
        Args:
            flight_name: Name of the flight (from --name arg).
            bucket_name: S3 bucket name.
            api_url: API URL for webhooks.
            dispatcher: TelemetryDispatcher for cellular status.
        """
        self.flight_name = flight_name
        self.bucket_name = bucket_name
        self.api_url = api_url
        self.dispatcher = dispatcher
        
        if not self.bucket_name:
            logger.warning("BUCKET_NAME not set, S3 uploads will be skipped")
        if not self.api_url:
            logger.warning("API_URL not set, webhooks will be skipped")
        
        # Create flight folder
        self.flight_folder = f"./{flight_name}"
        os.makedirs(self.flight_folder, exist_ok=True)
        
        # Phase-specific capture intervals (seconds)
        self.capture_intervals = {
            FlightPhase.GROUND: 5,
            FlightPhase.ASCENT_LOW: 30,
            FlightPhase.ASCENT_HIGH: 30,
            FlightPhase.NEAR_SPACE: 5,
            FlightPhase.DESCENT: 60,
            FlightPhase.LANDED: 2,
        }
        
        # Last capture times
        self.last_capture_times = {phase: 0 for phase in FlightPhase}
        
        # S3 client
        self.s3_client = boto3.client('s3')
        
        # Initialize camera
        self.camera = None
        if picamera_available:
            try:
                self.camera = Picamera2()
                # Configure for JPEG still image capture
                config = self.camera.create_still_configuration(
                    main={"format": "RGB888", "size": (1920, 1080)},
                    raw={"size": (4608, 2592)}
                )
                self.camera.configure(config)
                self.camera.start()
                logger.info("Camera initialized successfully (Picamera2)")
            except Exception as e:
                logger.error(f"Failed to initialize camera: {e}")
                self.camera = None
        else:
            logger.warning("Picamera2 not available. Photos will use: libcamera-still > PIL > minimal JPEG")

    def _get_disk_usage_percent(self) -> float:
        """Get current disk usage percentage."""
        stat = os.statvfs('/')
        return (1 - stat.f_bavail / stat.f_blocks) * 100

    def _delete_oldest_photos(self) -> None:
        """Delete oldest photos in flight folder until disk usage < 75%."""
        while self._get_disk_usage_percent() > 75:
            try:
                files = [f for f in os.listdir(self.flight_folder) if f.endswith('.jpg')]
                if not files:
                    break
                files.sort(key=lambda x: os.path.getctime(os.path.join(self.flight_folder, x)))
                oldest = files[0]
                os.remove(os.path.join(self.flight_folder, oldest))
                logger.info(f"Deleted oldest photo: {oldest}")
            except Exception as e:
                logger.error(f"Error deleting oldest photo: {e}")
                break

    def take_photo(self) -> Optional[str]:
        """
        Capture a photo and save locally.
        
        Returns:
            Filename of captured photo, or None if failed.
        """
        timestamp = int(time.time())
        filename = f"{timestamp}.jpg"
        filepath = os.path.join(self.flight_folder, filename)
        
        if self.camera:
            # Use Picamera2 if available
            try:
                stream = io.BytesIO()
                self.camera.capture_file(stream, format='jpeg')
                stream.seek(0)
                with open(filepath, 'wb') as f:
                    f.write(stream.getvalue())
                logger.info(f"Photo captured: {filename}")
                return filename
            except Exception as e:
                logger.error(f"Failed to capture photo with Picamera2: {e}")
                return None
        
        # Fallback 1: Try libcamera-still command-line tool (Raspberry Pi)
        try:
            result = subprocess.run(
                ['libcamera-still', '-o', filepath, '-t', '1'],
                capture_output=True,
                timeout=5,
                check=True
            )
            logger.info(f"Photo captured with libcamera-still: {filename}")
            return filename
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        
        # Fallback 2: Generate a realistic image using PIL
        if pil_available:
            try:
                img = Image.new('RGB', (1920, 1080), color=(73, 109, 137))
                draw = ImageDraw.Draw(img)
                
                # Add some visual content
                timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")
                draw.text((50, 50), f"Captured: {timestamp_str}", fill=(255, 255, 255))
                draw.text((50, 100), f"Flight: {self.flight_name}", fill=(255, 255, 255))
                
                # Save as JPEG
                img.save(filepath, 'JPEG', quality=90)
                logger.info(f"Photo generated with PIL: {filename}")
                return filename
            except Exception as e:
                logger.error(f"Failed to generate photo with PIL: {e}")
        
        # Fallback 3: Create minimal JPEG with proper markers
        logger.warning("Using minimal JPEG fallback")
        try:
            jpeg_header = bytes([0xFF, 0xD8])  # JPEG SOI marker
            jpeg_trailer = bytes([0xFF, 0xD9])  # JPEG EOI marker
            
            with open(filepath, 'wb') as f:
                f.write(jpeg_header)
                app0 = bytes([0xFF, 0xE0, 0x00, 0x10])
                app0 += b'JFIF\x00' + bytes([0x01, 0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00])
                f.write(app0)
                f.write(jpeg_trailer)
            
            logger.info(f"Minimal photo created: {filename}")
            return filename
        except Exception as e:
            logger.error(f"Failed to create minimal photo: {e}")
            return None

    def upload_to_s3(self, filename: str) -> Optional[str]:
        """
        Upload photo to S3.
        
        Args:
            filename: Name of the photo file.
            
        Returns:
            S3 URL if successful, None otherwise.
        """
        if not self.bucket_name:
            logger.warning("BUCKET_NAME not set, skipping S3 upload")
            return None
        
        filepath = os.path.join(self.flight_folder, filename)
        s3_key = f"{self.flight_name}/{filename}"
        
        try:
            self.s3_client.upload_file(filepath, self.bucket_name, s3_key)
            s3_url = f"https://{self.bucket_name}.s3.amazonaws.com/{s3_key}"
            logger.info(f"Uploaded to S3: {s3_url}")
            return s3_url
        except Exception as e:
            logger.error(f"Failed to upload {filename} to S3: {e}")
            return None

    def send_webhook(self, filename: str, s3_url: str) -> bool:
        """
        Send webhook notification after successful S3 upload.
        
        Args:
            filename: Photo filename.
            s3_url: S3 URL of the photo.
            
        Returns:
            True if successful, False otherwise.
        """
        if not self.api_url:
            logger.warning("API_URL not set, skipping webhook")
            return False
        
        try:
            payload = {
                "filename": filename,
                "s3_url": s3_url,
            }
            url = urllib.parse.urljoin(self.api_url.rstrip('/') + '/', "api/photo/notify/")
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info(f"Webhook sent for {filename}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send webhook for {filename}: {e}")
            return False

    def process_pending_uploads(self) -> None:
        """Upload all pending photos that failed previously."""
        if not self.dispatcher.is_cellular_enabled:
            return
        
        try:
            files = [f for f in os.listdir(self.flight_folder) if f.endswith('.jpg')]
            for filename in files:
                filepath = os.path.join(self.flight_folder, filename)
                if os.path.exists(filepath):
                    s3_url = self.upload_to_s3(filename)
                    if s3_url:
                        self.send_webhook(filename, s3_url)
                        # Remove local file after successful upload
                        os.remove(filepath)
                        logger.info(f"Removed local file after upload: {filename}")
        except Exception as e:
            logger.error(f"Error processing pending uploads: {e}")

    def capture_and_upload(self, phase: FlightPhase) -> None:
        """
        Capture photo if interval has passed, upload if possible.
        
        Args:
            phase: Current flight phase.
        """
        current_time = time.time()
        interval = self.capture_intervals.get(phase, 30)
        
        if current_time - self.last_capture_times[phase] < interval:
            return
        
        # Check disk usage before capturing
        if self._get_disk_usage_percent() > 75:
            self._delete_oldest_photos()
        
        filename = self.take_photo()
        if not filename:
            return
        
        self.last_capture_times[phase] = current_time
        
        # Try to upload immediately if cellular enabled
        if self.dispatcher.is_cellular_enabled:
            s3_url = self.upload_to_s3(filename)
            if s3_url:
                self.send_webhook(filename, s3_url)
                # Remove local file
                os.remove(os.path.join(self.flight_folder, filename))
            else:
                logger.info(f"Upload failed, keeping {filename} for later")
        else:
            logger.info(f"Offline, saved {filename} locally")


class FlightComputer:
    """Main flight computer logic."""

    def __init__(self, flight_name: str, descent_threshold: int = 3):
        """
        Initialize flight computer.
        
        Args:
            flight_name: Name of the flight.
            descent_threshold: Number of consecutive readings to trigger descent phase.
        """
        self.flight_name = flight_name
        bucket_name = os.getenv("S3_BUCKET_NAME")
        api_url = os.getenv("API_URL")
        
        self.sensor_manager = SensorManager()
        self.dispatcher = TelemetryDispatcher()
        self.camera_manager = CameraManager(flight_name, bucket_name, api_url, self.dispatcher)
        self.safety_manager = SafetyManager(self.dispatcher)
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

        start_time = time.time()
        iteration = 0
        last_phase = None
        is_mock_flight = not self.sensor_manager.use_real_gps

        try:
            while time.time() - start_time < duration:
                iteration += 1
                current_time = time.time()
                elapsed_time = current_time - start_time

                # --- MOCK FLIGHT SCENARIO ORCHESTRATION ---
                if is_mock_flight:
                    # After 5 minutes (300s), start descent and reconnection
                    if elapsed_time >= 300 and not self.sensor_manager.simulating_descent:
                        self.sensor_manager.start_descent_simulation()
                        logger.info("MOCK: Reconnected. Attempting to dump saved log to API...")
                        threading.Thread(target=self.dispatcher.dump_log_to_api, daemon=True).start()

                # Get sensor data
                gps = self.sensor_manager.get_gps()
                telemetry = self.sensor_manager.get_telemetry()

                # Update flight phase, with special pre-launch for mock flights
                if is_mock_flight and elapsed_time < 60:
                    phase = FlightPhase.GROUND
                    # During mock pre-launch, keep altitude at 0 to prevent phase change
                    telemetry.altitude = 0.0
                    self.sensor_manager.altitude = 0.0 # Reset for next iteration
                else:
                    phase = self.update_phase(telemetry.altitude)

                # Capture and upload photos based on phase
                self.camera_manager.capture_and_upload(phase)

                # --- MOCK FLIGHT: Radio silence simulation ---
                mock_radio_silence = is_mock_flight and 120 <= elapsed_time < 300

                # Handle phase transitions
                if phase != last_phase:
                    logger.info(f"Phase transition: {last_phase} -> {phase.value}")
                    
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
                if phase in (FlightPhase.GROUND, FlightPhase.ASCENT_LOW, FlightPhase.ASCENT_HIGH):
                    # Send telemetry every loop interval (every 5 seconds)
                    if current_time - self.dispatcher.last_send_time >= self.dispatcher.send_interval:
                        if mock_radio_silence:
                            logger.info("MOCK: Radio silence. Saving to log instead of sending.")
                            self.dispatcher.save_to_log(telemetry, gps, phase)
                        else:
                            self.dispatcher.send_data(telemetry, gps, phase)
                        self.dispatcher.last_send_time = current_time

                elif phase == FlightPhase.NEAR_SPACE:
                    # Save to local log (offline mode)
                    self.dispatcher.save_to_log(telemetry, gps, phase)

                elif phase in (FlightPhase.DESCENT, FlightPhase.LANDED):
                    # Attempt to send in real-time
                    self.dispatcher.send_data(telemetry, gps, phase)
                
                # Process any pending photo uploads
                self.camera_manager.process_pending_uploads()
                
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


def run_modem_cmd(at_command, timeout=5):
    """Sends an AT command and returns the output."""
    try:
        # We use socat because it handles the serial handshake reliably for us.
        # Use 'timeout' in the shell command to properly kill socat if it hangs.
        cmd = f"echo '{at_command}' | sudo timeout {timeout} socat -t 1 - /dev/ttyUSB2,crnl"
        result = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
        return result.decode().strip()
    except subprocess.CalledProcessError as e:
        return f"ERROR: {e.output.decode() if e.output else 'Timeout'}"
    except Exception as e:
        return f"SYSTEM FAILURE: {str(e)}"

def initialize_flight_gps():
    print("--- 🛰️ SCOUT BALLOON GPS INITIALIZATION ---")
    
    # 1. Wait for Network Manager
    print("[1/5] Checking for Internet/Cellular connection...")
    connected = False
    for attempt in range(1, 11):
        status = subprocess.getoutput("nmcli -t -f STATE g")
        if status == "connected":
            print(" ✅ Internet Connected! Time sync is now possible.")
            connected = True
            break
        else:
            print(f" ⏳ Waiting for Network Manager (Attempt {attempt}/10)...")
            time.sleep(5)
            
    if not connected:
        print(" ❌ WARNING: No network found. GPS will lack 'Assisted' data and take longer to lock.")

    # 2. Check for Modem hardware
    if not os.path.exists("/dev/ttyUSB2"):
        print("[2/5] Modem not found! Attempting hardware wake-up (GPIO 22)...")
        subprocess.run("sudo pinctrl set 22 op && sudo pinctrl set 22 dh", shell=True)
        time.sleep(2)
        subprocess.run("sudo pinctrl set 22 dl", shell=True)
        print(" ⏳ Waiting 15 seconds for modem to boot...")
        time.sleep(15)

    # 3. Start GPS Engine
    print("[3/5] Powering up GPS hardware...")
    run_modem_cmd("ATE0")       # Turn off local echo
    run_modem_cmd("AT+CMEE=1")  # Enable numeric error codes
    response = run_modem_cmd("AT+QGPS=1")
    if "OK" in response or "504" in response: # 504 means already on
        print(" ✅ GPS Engine Active.")
    else:
        print(f" ❌ FAILED to start GPS: {response}")
        return False

    # 4. Sync Time and Assisted GPS (The 'Speed Boost')
    print("[4/5] Syncing satellite almanac and flight time...")
    run_modem_cmd('AT+QNTP=1,"pool.ntp.org"')
    run_modem_cmd('AT+QGPSXTRA=1')
    run_modem_cmd('AT+QGPSXTRADATA=1')
    print(" ✅ Time & Almanac data injected.")

    # 5. Set Airborne Mode (The 'Safety Switch')
    print("[5/5] Configuring Flight Mode (Airborne < 1g)...")
    response = run_modem_cmd('AT+QGPSCFG="dynamicmodel",6')
    if "OK" in response:
        print(" ✅ Airborne Mode set! GPS will not lock out at high altitude.")
    else:
        print(f" ❌ WARNING: Could not set Airborne mode. Accuracy may drop above 12km.")

    print("\\n--- 🎈 INITIALIZATION COMPLETE: READY FOR LAUNCH ---")
    print("Point the sticker antenna at the sky and check signal with 'cat /dev/ttyUSB1'")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Eclipse Balloon Flight Computer")
    parser.add_argument("--name", type=str, help="Override the balloon ID/name from .env")
    parser.add_argument("--mock", action="store_true", help="Run with mock sensor data and skip hardware initialization.")
    args = parser.parse_args()

    if not args.mock:
        if not initialize_flight_gps():
            print("Critical failure during setup. Check connections and try again.")
            sys.exit(1)
    else:
        os.environ["USE_REAL_GPS"] = "false"
        print("--- 🚀 RUNNING IN MOCK MODE - NO HARDWARE REQUIRED ---")

    if args.name:
        os.environ["BALLOON_ID"] = args.name

    flight_name = args.name or os.getenv("BALLOON_ID")
    if not flight_name:
        print("Error: Flight name not set. Use --name or set BALLOON_ID in .env")
        sys.exit(1)

    flight_computer = FlightComputer(flight_name)
    flight_computer.run()
